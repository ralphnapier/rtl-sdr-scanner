import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import wave
import test_v057

spec = importlib.util.spec_from_file_location('hunter058', Path(__file__).resolve().parents[1] / 'rf_hunter_v058_recording_metadata.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class AudioRegressionTests(test_v057.AudioTests):
    def setUp(self):
        self.patcher = patch.object(test_v057, 'h', h)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()


def make_wav(path, seconds=1):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(32000)
        wav.writeframes(bytes(round(seconds*32000)*2))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        h.pygame.quit()
        self.temp.cleanup()

    def test_metadata_matches_written_audio_including_preroll(self):
        with patch.object(h, 'RECORDINGS_DIR', self.directory):
            r = h.ActivityRecorder(125_012_500, 8, 19.7)
            values = [0., 0.] + [10., 12., 14., 16., 18., 20., 22.]
            for value in values:
                r.feed(bytes(2048), value)
            self.assertTrue(r.active)
            r.close()
        path = next(self.directory.glob('*.wav'))
        meta = json.loads(path.with_suffix('.json').read_text())
        self.assertEqual(meta['frequency_hz'], 125_012_500)
        self.assertEqual(meta['peak_signal_db'], 22)
        self.assertAlmostEqual(meta['mean_signal_db'], sum(values)/len(values))
        self.assertAlmostEqual(meta['duration_seconds'], len(values)*.032)
        self.assertTrue(meta['complete'])
        item = h.recording_info(path)
        self.assertEqual(item['frequency'], 125.0125)
        self.assertEqual(item['peak'], 22)
        self.assertEqual(item['duration'], meta['duration_seconds'])
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_split_metadata_resets_signal_statistics(self):
        with patch.object(h, 'RECORDINGS_DIR', self.directory), patch.object(h, 'MAX_RECORD_SECONDS', .192):
            r = h.ActivityRecorder(125_000_000, 8)
            for value in [10.]*6 + [30.]*6:
                r.feed(bytes(2048), value)
            r.close()
        metadata = [json.loads(p.read_text()) for p in sorted(self.directory.glob('*.json'))]
        self.assertEqual([m['peak_signal_db'] for m in metadata], [10., 30.])
        self.assertEqual([m['mean_signal_db'] for m in metadata], [10., 30.])

    def test_legacy_and_corrupt_metadata_are_unknown(self):
        path = self.directory / 'RF_20261111_120000_125.000MHz_001_AM.wav'
        make_wav(path)
        legacy = h.recording_info(path)
        self.assertIsNone(legacy['peak'])
        self.assertEqual(legacy['frequency'], 125.)
        self.assertEqual(legacy['duration'], 1.)
        path.with_suffix('.json').write_text('{broken')
        self.assertIsNone(h.recording_info(path)['peak'])
        path.with_suffix('.json').write_text(json.dumps({'schema_version': 1, 'complete': True,
            'signal_units': 'dB above local noise', 'peak_signal_db': float('nan')}))
        self.assertIsNone(h.recording_info(path)['peak'])

    def test_numeric_sort_unknown_last_and_filters(self):
        items = []
        for i, peak in enumerate([9., 20., None, 100.]):
            items.append(dict(name=str(i), peak=peak, mean=peak, frequency=125.+i,
                              timestamp=i, duration=4-i, modulation='AM' if i<2 else 'NFM'))
        self.assertEqual([v['peak'] for v in h.filter_recordings(items)], [100, 20, 9, None])
        self.assertEqual([v['peak'] for v in h.filter_recordings(items, descending=False)], [9, 20, 100, None])
        self.assertEqual([v['peak'] for v in h.filter_recordings(items, modulation='AM', min_db=10)], [20])
        self.assertEqual(len(h.filter_recordings(items, frequency_text='125')), 1)

    def test_touch_browse_play_pause_stop_and_missing_file(self):
        path = self.directory / 'RF_20261111_120000_125.000MHz_001_AM.wav'
        make_wav(path)
        h.pygame.init()
        screen = h.pygame.display.set_mode((640, 480))
        audio = h.AudioOutput()
        audio.reset()
        before_play = Mock()
        browser = h.RecordingBrowser(screen, audio, self.directory, before_play)
        deadline = time.monotonic()+3
        while browser.loading and time.monotonic()<deadline:
            browser.draw()
            time.sleep(.01)
        self.assertEqual(len(browser.visible), 1)
        browser.tap(50, 165)
        browser.draw()
        self.assertEqual(browser.selected['path'], path)
        browser.tap(50, 275)
        before_play.assert_called_once()
        self.assertEqual(browser.playing, path)
        browser.action('pause')
        self.assertTrue(browser.paused)
        browser.action('stop')
        self.assertIsNone(browser.playing)
        path.unlink()
        browser.action('play')
        self.assertIn('Cannot play', browser.notice)
        browser.action('back')
        browser.action('freq')
        browser.action('1')
        browser.action('2')
        browser.action('enter')
        self.assertEqual(browser.frequency_text, '12')
        self.assertTrue(browser.action('back'))

    def test_standalone_never_starts_radio(self):
        with patch.object(h, 'RadioEngine') as radio, patch.object(h, 'DemoEngine') as demo, patch('sys.argv', ['hunter', '--recordings-only', '--windowed', '--recordings-dir', str(self.directory)]), patch.object(h.pygame.event, 'get', return_value=[h.pygame.event.Event(h.pygame.QUIT)]):
            h.main()
        radio.assert_not_called()
        demo.assert_not_called()


if __name__ == '__main__':
    unittest.main()
