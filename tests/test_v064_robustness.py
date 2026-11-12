"""Regression checks for saved-name integrity and stalled WIDE scans.

Extract the small pieces under test so these checks run on a PC without
NumPy, pygame, or an RTL-SDR attached.
"""
import ast
import json
import os
import select
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / 'rf_hunter_v064_robustness.py'


def extracted_node(name, kind):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    return next(node for node in tree.body if isinstance(node, kind) and node.name == name)


def frequency_names_class():
    node = extracted_node('FrequencyNames', ast.ClassDef)
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = {'Path': Path, 'json': json,
                 'FREQUENCIES_FILE': Path('frequencies.json')}
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace['FrequencyNames']


class SavedNamesRobustnessTests(unittest.TestCase):
    def test_corrupt_file_is_not_overwritten_by_next_save(self):
        FrequencyNames = frequency_names_class()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'frequencies.json'
            original = b'{ "frequencies": [BROKEN JSON\n'
            path.write_bytes(original)
            try:
                FrequencyNames(path).add(146_940_000)
            except (OSError, ValueError):
                pass  # A visible save refusal is a valid recovery behavior.
            self.assertEqual(path.read_bytes(), original)

    def test_complete_refresh_clears_stale_or_ambiguous_online_names(self):
        FrequencyNames = frequency_names_class()
        with tempfile.TemporaryDirectory() as directory:
            names = FrequencyNames(Path(directory) / 'frequencies.json')
            for hz in (146_940_000, 146_970_000, 147_000_000):
                names.add(hz)
            def repeater(mhz, call):
                return {'frequency': mhz, 'callsign': call, 'city': 'Florence',
                        'mode': 'FM', 'status': 'active'}
            names.merge_online([repeater(146.94, 'K4ONE'),
                                repeater(146.97, 'K4TWO'),
                                repeater(147.0, 'K4THREE')])
            names.rename(147_000_000, 'My manual label')
            self.assertEqual(names.label_for(146_940_000), 'K4ONE Florence')
            self.assertEqual(names.label_for(146_970_000), 'K4TWO Florence')

            # The completed new response removed one repeater and has two
            # different callsigns on the other frequency.
            names.merge_online([repeater(146.97, 'K4TWO'),
                                repeater(146.97, 'K4FOUR')])
            self.assertEqual(names.label_for(146_940_000), '')
            self.assertEqual(names.label_for(146_970_000), '')
            self.assertEqual(names.label_for(147_000_000), 'My manual label')


class WideWatchdogTests(unittest.TestCase):
    def test_malformed_csv_bytes_cannot_keep_receiver_alive_forever(self):
        radio = extracted_node('RadioEngine', ast.ClassDef)
        wide = next(node for node in radio.body
                    if isinstance(node, ast.FunctionDef) and node.name == '_wide')
        module = ast.fix_missing_locations(ast.Module(body=[wide], type_ignores=[]))

        class Clock:
            now = 0
            def monotonic(self):
                self.now += 2
                if self.now > 500:
                    raise AssertionError('WIDE never timed out on invalid CSV')
                return self.now
        clock = Clock()
        stdout = types.SimpleNamespace(fileno=lambda: 1)
        process = types.SimpleNamespace(stdout=stdout, poll=lambda: None)
        radio_key = ('wide', 162_000_000, 19.7, 0, False, 162_000_000)
        engine = types.SimpleNamespace(
            stop=types.SimpleNamespace(is_set=lambda: False),
            _current=lambda: (radio_key, 0),
            threshold_db=8., last_publish=0., wide_sweeps=0)
        namespace = {
            'PRESETS': [('TEST', 144., 148., 10, 4)],
            'time': clock,
            'select': types.SimpleNamespace(select=lambda *_: ([stdout], [], [])),
            'os': types.SimpleNamespace(read=lambda *_: b'bad,csv,line\n'),
            'parse_power_line': lambda _line: None,
        }
        exec(compile(module, str(SOURCE), 'exec'), namespace)
        problem = namespace['_wide'](engine, process, radio_key)
        self.assertIsInstance(problem, str)
        self.assertTrue(problem)
        self.assertLessEqual(clock.now, 500)


class ReceiverCleanupTests(unittest.TestCase):
    def test_stdout_close_error_does_not_escape_cleanup(self):
        radio = extracted_node('RadioEngine', ast.ClassDef)
        stop = next(node for node in radio.body
                    if isinstance(node, ast.FunctionDef) and node.name == '_stop_process')
        stop.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[stop], type_ignores=[]))
        namespace = {'subprocess': subprocess}
        exec(compile(module, str(SOURCE), 'exec'), namespace)
        def broken_close():
            raise OSError('stream already closed')
        proc = types.SimpleNamespace(
            poll=lambda: 0, wait=lambda timeout: 0,
            stdout=types.SimpleNamespace(close=broken_close))
        namespace['_stop_process'](proc)  # The receiver worker must survive.

    def test_receiver_log_rotates_before_new_attempt(self):
        radio = extracted_node('RadioEngine', ast.ClassDef)
        run = next(node for node in radio.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'run')
        module = ast.fix_missing_locations(ast.Module(body=[run], type_ignores=[]))
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'receiver.log'
            with log.open('wb') as handle:
                handle.truncate(5 * 1024 * 1024)
            stop = threading.Event()
            key = ('wide', 162_000_000, 19.7, 0, False, 162_000_000)
            def fake_wide(_proc, _key):
                stop.set()
                return None
            engine = types.SimpleNamespace(
                stop=stop, RETUNE_DELAY=0., lock=threading.Lock(),
                target=key, wide_sweeps=0,
                _current=lambda: (key, time.monotonic() - 1),
                _wide=fake_wide, _stop_process=lambda _proc: None)
            namespace = {
                'RADIO_LOG': log,
                'PRESETS': [('TEST', 144., 148., 10, 4)],
                'rtl_gain_args': lambda _gain: [],
                'subprocess': types.SimpleNamespace(
                    Popen=lambda *_args, **_kwargs: object(),
                    DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE),
                'time': time, 'os': os, 'select': select, 'sys': __import__('sys'),
            }
            exec(compile(module, str(SOURCE), 'exec'), namespace)
            namespace['run'](engine)
            rotated = log.with_name('receiver.log.1')
            self.assertTrue(rotated.exists())
            self.assertEqual(rotated.stat().st_size, 5 * 1024 * 1024)
            self.assertIn(b'rtl_power', log.read_bytes())


if __name__ == '__main__':
    unittest.main()
