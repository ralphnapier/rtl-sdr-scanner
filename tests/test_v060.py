import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import test_v057
import test_v058
import test_v059

spec = importlib.util.spec_from_file_location('hunter060', Path(__file__).resolve().parents[1] / 'rf_hunter_v060_encoder.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class AudioRegressionTests(test_v057.AudioTests):
    def setUp(self):
        self.patcher = patch.object(test_v057, 'h', h)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()


class RotationRegressionTests(test_v059.RotationTests):
    def setUp(self):
        self.patcher = patch.object(test_v059, 'h', h)
        self.patcher.start()

    def tearDown(self):
        super().tearDown()
        self.patcher.stop()


class LibraryRegressionTests(test_v058.LibraryTests):
    def setUp(self):
        self.patcher = patch.object(test_v058, 'h', h)
        self.patcher.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.patcher.stop()


class PeakTests(unittest.TestCase):
    def setUp(self):
        self.f = list(range(162_500_000, 162_601_000, 1000))
        self.p = [-60.] * len(self.f)

    def test_nearest_not_strongest_and_stays_on_peak(self):
        self.p[self.f.index(162_553_000)] = -40
        self.p[self.f.index(162_560_000)] = -10
        self.assertEqual(h.nearest_signal_peak(162_550_000, self.f, self.p), 162_553_000)
        self.assertEqual(h.nearest_signal_peak(162_553_000, self.f, self.p), 162_553_000)

    def test_weak_distant_missing_and_dc_gap(self):
        self.p[self.f.index(162_553_000)] = -54
        self.p[self.f.index(162_563_000)] = -10
        self.assertIsNone(h.nearest_signal_peak(162_550_000, self.f, self.p))
        self.assertIsNone(h.nearest_signal_peak(162_550_000, None, None))
        self.p[self.f.index(162_553_000)] = -40
        self.assertIsNone(h.nearest_signal_peak(162_550_000, self.f, self.p, threshold_db=25))
        self.assertIsNone(h.nearest_signal_peak(162_550_000, self.f[::10], self.p[::10]))
        pairs = [(f,p) for f,p in zip(self.f,self.p) if not 162_545_000 <= f <= 162_552_000]
        f, p = zip(*pairs)
        self.assertIsNone(h.nearest_signal_peak(162_550_000, f, p))


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.]
        self.encoder = h.EncoderInput(clock=lambda: self.now[0])
        self.encoder.closed = False

    def tearDown(self):
        self.encoder.close()

    def test_click_hold_and_boundary_never_double_fire(self):
        self.encoder.press()
        self.now[0] = .1
        self.encoder.release()
        self.assertEqual(self.encoder.drain(), [('snap', 0)])
        self.encoder.press()
        self.now[0] = 1.
        self.encoder.hold()
        self.encoder.hold()
        self.encoder.release()
        self.assertEqual(self.encoder.drain(), [('step', 0)])
        self.encoder.press()
        self.now[0] = 2.
        self.encoder.release()  # release wins race with held callback
        self.encoder.hold()
        self.assertEqual(self.encoder.drain(), [('step', 0)])
        self.encoder.release()
        self.assertEqual(self.encoder.drain(), [])

    def test_direction_bounded_queue_staleness_and_shutdown(self):
        self.encoder.turn(1)
        self.encoder.reverse = True
        self.encoder.turn(1)
        self.assertEqual(self.encoder.drain(), [('turn',1), ('turn',-1)])
        worker = threading.Thread(target=lambda: [self.encoder.turn(1) for _ in range(1000)])
        worker.start()
        worker.join()
        self.assertEqual(len(self.encoder.events), 128)
        self.now[0] = 2
        self.assertEqual(self.encoder.drain(), [])
        self.encoder.close()
        self.encoder.turn(1)
        self.assertEqual(self.encoder.drain(), [])

    def test_chip_discovery_uses_label_not_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, label in [(0,'other-controller'), (4,'pinctrl-rp1')]:
                chip = root / f'gpiochip{number}'
                chip.mkdir()
                (chip / 'label').write_text(label)
            self.assertEqual(h.discover_gpiochip(root), 4)
            (root/'gpiochip4'/'label').write_text('unrelated')
            with self.assertRaises(OSError):
                h.discover_gpiochip(root)

    def test_real_gpiozero_quadrature_and_partial_bounce(self):
        from gpiozero.pins.mock import MockFactory
        factory = MockFactory()
        factory.chip = 0
        module = types.ModuleType('gpiozero.pins.lgpio')
        module.LGPIOFactory = lambda chip: factory
        with patch.dict(sys.modules, {'gpiozero.pins.lgpio': module}):
            self.encoder.start(0)
        self.assertFalse(self.encoder.closed, self.encoder.error)
        a, b = factory.pin(17), factory.pin(27)
        # A complete CW quadrature cycle; a partial bounce must not count.
        a.drive_low(); a.drive_high()
        self.assertEqual(self.encoder.drain(), [])
        a.drive_low(); b.drive_low(); a.drive_high(); b.drive_high()
        self.assertEqual(self.encoder.drain(), [('turn',1)])
        b.drive_low(); a.drive_low(); b.drive_high(); a.drive_high()
        self.assertEqual(self.encoder.drain(), [('turn',-1)])
        sw = factory.pin(22)
        sw.drive_low()
        self.now[0] = .15
        sw.drive_high()
        self.assertEqual(self.encoder.drain(), [('snap',0)])
        rotor, button = self.encoder.rotor, self.encoder.button
        self.encoder.close()
        self.assertTrue(rotor.closed)
        self.assertTrue(button.closed)

    def test_partial_startup_failure_releases_resources(self):
        from gpiozero.pins.mock import MockFactory
        factory = MockFactory()
        factory.chip = 0
        factory.close = Mock(wraps=factory.close)
        module = types.ModuleType('gpiozero.pins.lgpio')
        module.LGPIOFactory = lambda chip: factory
        with patch.dict(sys.modules, {'gpiozero.pins.lgpio': module}), patch('gpiozero.Button', side_effect=OSError('GPIO22 busy')):
            self.encoder.start(0)
        self.assertTrue(self.encoder.closed)
        self.assertIsNone(self.encoder.rotor)
        self.assertIn('GPIO22 busy', self.encoder.error)
        factory.close.assert_called_once()


class DialUITests(unittest.TestCase):
    def test_click_waits_for_new_spectrum_and_later_turn_cancels_it(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                frame = [0]
                created = []
                class Engine(h.DemoEngine):
                    def __init__(self, *args):
                        super().__init__(*args)
                        self.changes = []
                        created.append(self)
                    def change(self, **kwargs):
                        self.changes.append((frame[0], kwargs['listen_hz']))
                        return super().change(**kwargs)
                    def snapshot(self):
                        if frame[0] < 6:
                            return None, self.version, 'TUNING', '', None, []
                        data = h.np.full(h.FFT_SIZE, -60., dtype=h.np.float32)
                        data[h.np.argmin(abs(self.target[1]+h.FFT_FREQS-162_550_000))] = -30
                        self.version += 1
                        return ('live','',data), self.version, 'RECEIVING', '', None, []
                batches = {2:[('turn',1),('snap',0)]}
                if cancel:
                    batches[3] = [('turn',1)]
                encoder = Mock()
                encoder.reverse = False
                encoder.drain.side_effect = lambda: batches.get(frame[0], [])
                def events():
                    frame[0] += 1
                    return [h.pygame.event.Event(h.pygame.QUIT)] if frame[0] == 8 else []
                with tempfile.TemporaryDirectory() as directory, patch.object(h, 'SETTINGS_FILE', Path(directory)/'settings.json'), patch.object(h, 'EncoderInput', return_value=encoder), patch.object(h, 'DemoEngine', Engine), patch.object(h.pygame.event, 'get', side_effect=events), patch('sys.argv',['hunter','--demo','--windowed','--center','162.550']):
                    h.main()
                expected = [(2,162_551_000), (3,162_552_000)] if cancel else [(2,162_551_000),(6,162_550_000)]
                self.assertEqual(created[0].changes, expected)

    def test_terminal_diagnostic_does_not_start_radio_or_display(self):
        encoder = Mock()
        encoder.closed = False
        encoder.status = 'GPIO ready'
        encoder.drain.return_value = [('turn',1),('snap',0),('step',0)]
        with patch.object(h, 'EncoderInput', return_value=encoder), patch.object(h, 'RadioEngine') as radio, patch.object(h.pygame, 'init') as display, patch.object(h.time, 'sleep', side_effect=KeyboardInterrupt), patch('sys.argv',['hunter','--encoder-test']), patch('builtins.print') as output:
            h.main()
        radio.assert_not_called()
        display.assert_not_called()
        encoder.close.assert_called_once()
        self.assertIn('CLICK / SNAP', [c.args[0] for c in output.call_args_list])

    def test_turn_batch_hold_and_snap_use_listen_frequency(self):
        created = []
        frame = [0]
        class Engine(h.DemoEngine):
            def __init__(self, *args):
                super().__init__(*args)
                self.changes = []
                created.append(self)
            def change(self, **kwargs):
                self.changes.append(kwargs)
                return super().change(**kwargs)
            def snapshot(self):
                data = h.np.full(h.FFT_SIZE, -60., dtype=h.np.float32)
                frequency = self.target[1] + h.FFT_FREQS
                data[h.np.argmin(abs(frequency-162_550_000))] = -30
                self.version += 1
                return ('live','2026-11-11 12:00:00',data), self.version, 'RECEIVING', '', None, []
        batches = {3:[('turn',1)]*3, 5:[('step',0)], 7:[('turn',-1)], 9:[('snap',0)]}
        encoder = Mock()
        encoder.reverse = False
        encoder.drain.side_effect = lambda: batches.get(frame[0], [])
        def events():
            frame[0] += 1
            return [h.pygame.event.Event(h.pygame.QUIT)] if frame[0] == 12 else []
        with tempfile.TemporaryDirectory() as directory, patch.object(h, 'SETTINGS_FILE', Path(directory)/'settings.json'), patch.object(h, 'EncoderInput', return_value=encoder), patch.object(h, 'DemoEngine', Engine), patch.object(h.pygame.event, 'get', side_effect=events), patch('sys.argv',['hunter','--demo','--windowed','--center','162.550']):
            h.main()
            self.assertEqual(h.load_settings()['tune_step_hz'], 2500)
        self.assertEqual([x['listen_hz'] for x in created[0].changes], [162_553_000,162_550_500,162_550_000])
        encoder.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
