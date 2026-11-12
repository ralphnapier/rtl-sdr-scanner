"""Receiver recovery tests use synthetic USB entries and fake processes."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'hunter061', Path(__file__).resolve().parents[1] / 'rf_hunter_v061_recovery.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class UsbRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sysfs = self.root / 'sys'
        self.devices = self.root / 'dev'
        self.sysfs.mkdir()
        self.devices.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def add_device(self, name, vendor='0bda', product='2838', bus=1, number=2):
        directory = self.sysfs / name
        directory.mkdir()
        for filename, value in [('idVendor', vendor), ('idProduct', product),
                                ('busnum', str(bus)), ('devnum', str(number))]:
            (directory / filename).write_text(value)
        dev = self.devices / f'{bus:03d}'
        dev.mkdir(exist_ok=True)
        (dev / f'{number:03d}').write_bytes(b'')
        return dev / f'{number:03d}'

    def test_exactly_one_matching_usb_device_is_required(self):
        other = self.add_device('keyboard', vendor='046d')
        with self.assertRaises(OSError):
            h.find_sdr_usb_device(self.sysfs, self.devices)
        sdr = self.add_device('dongle', bus=2, number=7)
        self.assertEqual(h.find_sdr_usb_device(self.sysfs, self.devices), sdr)
        self.add_device('second-sdr', bus=3, number=8)
        with self.assertRaises(OSError):
            h.find_sdr_usb_device(self.sysfs, self.devices)
        self.assertTrue(other.exists())

    def test_reset_issues_ioctl_only_for_selected_dongle(self):
        selected = self.add_device('dongle', bus=2, number=7)
        self.add_device('mouse', vendor='046d', bus=3, number=4)
        fake_fcntl = types.ModuleType('fcntl')
        fake_fcntl.ioctl = Mock(return_value=0)
        with patch.object(h.platform, 'system', return_value='Linux'), patch.dict(sys.modules, {'fcntl': fake_fcntl}):
            self.assertEqual(h.reset_sdr_usb_device(self.sysfs, self.devices), str(selected))
        fake_fcntl.ioctl.assert_called_once()
        self.assertEqual(fake_fcntl.ioctl.call_args.args[1:], (h.USB_RESET_IOCTL, 0))

    def test_non_linux_never_attempts_reset(self):
        self.add_device('dongle')
        with patch.object(h.platform, 'system', return_value='Windows'):
            with self.assertRaises(OSError):
                h.reset_sdr_usb_device(self.sysfs, self.devices)


class WorkerRecoveryTests(unittest.TestCase):
    def run_attempts(self, problems, reset_result='/dev/bus/usb/001/002', enabled=True):
        with tempfile.TemporaryDirectory() as directory, patch.object(h, 'previous_hits', return_value=[]):
            engine = h.RadioEngine(162_700_000, 19.7, 2, 'wide')
            engine.usb_recovery_enabled = enabled
            log = Path(directory) / 'rtl.log'
            process = Mock()
            process.returncode = 1
            count = [0]
            def wide(_proc, _key):
                count[0] += 1
                if count[0] == len(problems):
                    engine.stop.set()
                return problems[count[0]-1]
            reset = Mock(side_effect=reset_result if isinstance(reset_result, Exception) else None)
            if not isinstance(reset_result, Exception):
                reset.return_value = reset_result
            with patch.object(h, 'RADIO_LOG', log), patch.object(h.subprocess, 'Popen', return_value=process), patch.object(engine, '_wide', side_effect=wide), patch.object(engine, '_stop_process'), patch.object(engine.stop, 'wait', return_value=False), patch.object(h, 'reset_sdr_usb_device', reset):
                engine.run()
            return engine.usb_reset_count, reset.call_count, log.read_text()

    def test_repeated_csv_end_resets_once_then_retries(self):
        count, attempts, log = self.run_attempts(['rtl_power CSV stream ended (exit 1)']*5)
        self.assertEqual((count, attempts), (1, 1))
        self.assertIn('USB reset /dev/bus/usb/001/002; reopening SDR', log)

    def test_one_sweep_per_failed_attempt_still_recovers(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(h, 'previous_hits', return_value=[]):
            engine = h.RadioEngine(162_700_000, 19.7, 2, 'wide')
            process = Mock()
            count = [0]
            def wide(_proc, _key):
                count[0] += 1
                engine._publish(engine.target, 'wide', str(count[0]), ([], []))
                if count[0] == 5:
                    engine.stop.set()
                return 'rtl_power CSV stream ended (exit 1)'
            with patch.object(h, 'RADIO_LOG', Path(directory)/'rtl.log'), patch.object(h.subprocess, 'Popen', return_value=process), patch.object(engine, '_wide', side_effect=wide), patch.object(engine, '_stop_process'), patch.object(engine.stop, 'wait', return_value=False), patch.object(h, 'reset_sdr_usb_device', return_value='/dev/bus/usb/001/002') as reset:
                engine.run()
            reset.assert_called_once()

    def test_no_reset_before_third_failure_or_when_disabled(self):
        for enabled, problems in [(True, ['rtl_power CSV stream ended (exit 1)']*2),
                                  (False, ['rtl_power CSV stream ended (exit 1)']*5)]:
            with self.subTest(enabled=enabled):
                count, attempts, _ = self.run_attempts(problems, enabled=enabled)
                self.assertEqual((count, attempts), (0, 0))

    def test_failure_is_reported_and_software_retries_continue(self):
        count, attempts, log = self.run_attempts(['rtl_power CSV stream ended (exit 1)']*4,
                                                 reset_result=PermissionError('USB reset denied'))
        self.assertEqual((count, attempts), (0, 1))
        self.assertIn('USB reset unavailable: USB reset denied', log)

    def test_healthy_sweep_clears_failure_streak(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(h, 'previous_hits', return_value=[]):
            engine = h.RadioEngine(162_700_000, 19.7, 2, 'wide')
            process = Mock()
            count = [0]
            def wide(_proc, _key):
                count[0] += 1
                if count[0] == 3:
                    engine._publish(engine.target, 'wide', '2026-11-12 12:00:00', ([], []))
                    engine._publish(engine.target, 'wide', '2026-11-12 12:00:10', ([], []))
                if count[0] == 5:
                    engine.stop.set()
                return 'rtl_power CSV stream ended (exit 1)'
            with patch.object(h, 'RADIO_LOG', Path(directory)/'rtl.log'), patch.object(h.subprocess, 'Popen', return_value=process), patch.object(engine, '_wide', side_effect=wide), patch.object(engine, '_stop_process'), patch.object(engine.stop, 'wait', return_value=False), patch.object(h, 'reset_sdr_usb_device') as reset:
                engine.run()
            reset.assert_not_called()

    def test_csv_eof_waits_for_real_exit_code(self):
        with patch.object(h, 'previous_hits', return_value=[]):
            engine = h.RadioEngine(162_700_000, 19.7, 2, 'wide')
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 3
        with patch.object(h.select, 'select', return_value=([process.stdout], [], [])), patch.object(h.os, 'read', return_value=b''):
            self.assertEqual(engine._wide(process, engine.target), 'rtl_power CSV stream ended (exit 3)')

    def test_unwritable_log_does_not_prevent_receiver_start(self):
        with patch.object(h, 'previous_hits', return_value=[]):
            engine = h.RadioEngine(162_700_000, 19.7, 2, 'wide')
        process = Mock()
        def wide(_proc, _key):
            engine.stop.set()
            return None
        with patch.object(Path, 'open', side_effect=PermissionError('no log')), patch.object(h.subprocess, 'Popen', return_value=process) as start, patch.object(engine, '_wide', side_effect=wide), patch.object(engine, '_stop_process'):
            engine.run()
        start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
