import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import wave

os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'
spec = importlib.util.spec_from_file_location('hunter', Path(__file__).resolve().parents[1] / 'rf_hunter_v057_audio_controls.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class AudioTests(unittest.TestCase):
    def test_live_worker_listening_with_and_without_recording(self):
        for armed in (False, True):
            with tempfile.TemporaryDirectory() as directory, patch.object(h, 'RECORDINGS_DIR', Path(directory)):
                engine = h.RadioEngine(125_100_000, 19.7, 0)
                engine.change(listen_hz=125_000_000)
                engine.monitor = True
                engine.record_armed = armed
                engine.audio_output = Mock()
                proc = Mock()
                proc.poll.return_value = None
                calls = [0]
                def publish(*args):
                    calls[0] += 1
                    if calls[0] >= 12:
                        engine.stop.set()
                with patch.object(engine, '_publish', side_effect=publish), patch.object(h.select, 'select', return_value=([proc.stdout], [], [])), patch.object(h.os, 'read', return_value=bytes([127, 140]) * (h.BLOCK_BYTES // 2)), patch.object(h, 'channel_snr_db', return_value=20):
                    engine._live(proc, engine.target)
                self.assertEqual(engine.audio_output.feed.call_count, 12)
                self.assertEqual(len(list(Path(directory).glob('*.wav'))), int(armed))
                self.assertFalse(engine.recording_snapshot()['active'])

    def test_am_and_fm_demodulate_tone(self):
        n = h.np.arange(h.BLOCK_BYTES // 2)
        for freq in (125_000_000, 162_550_000):
            decoder = h.ChannelAudio(freq + 100_000, freq)
            pieces = []
            for block in range(12):
                t = (n + block * len(n)) / h.SR
                carrier = -2 * h.np.pi * 100_000 * t
                if decoder.demod == 'AM':
                    iq = .5 * (1 + .5 * h.np.sin(2*h.np.pi*1000*t)) * h.np.exp(1j*carrier)
                else:
                    iq = .6 * h.np.exp(1j*(carrier + 2.5*h.np.sin(2*h.np.pi*1000*t)))
                raw = h.np.empty(len(n)*2, dtype=h.np.uint8)
                raw[::2] = h.np.clip(iq.real*127.5+127.5, 0, 255)
                raw[1::2] = h.np.clip(iq.imag*127.5+127.5, 0, 255)
                pieces.append(decoder.demodulate(raw.tobytes()))
            pcm = h.np.frombuffer(b''.join(pieces[4:]), dtype='<i2').astype(float)
            hz = h.np.fft.rfftfreq(len(pcm), 1/h.AUDIO_RATE)[h.np.argmax(abs(h.np.fft.rfft(pcm)))]
            self.assertAlmostEqual(hz, 1000, delta=10)
            self.assertGreater(h.np.std(pcm), 100)

    def test_recording_rotation_and_low_disk(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(h, 'RECORDINGS_DIR', Path(directory)), patch.object(h, 'MAX_RECORD_SECONDS', .25):
            r = h.ActivityRecorder(125_000_000, 8)
            for _ in range(25):
                r.feed(bytes(2048), 20)
            r.close()
            files = list(Path(directory).glob('*.wav'))
            self.assertGreater(len(files), 1)
            for path in files:
                with wave.open(str(path)) as wav:
                    self.assertEqual(wav.getframerate(), 32000)
                    self.assertGreater(wav.getnframes(), 0)
            with patch.object(h.shutil, 'disk_usage') as usage:
                usage.return_value.free = 0
                r = h.ActivityRecorder(125_000_000, 8)
                for _ in range(6):
                    r.feed(bytes(2048), 20)
                self.assertTrue(r.disabled)
                self.assertFalse(r.active)

    def test_output_reset_mute_and_queue_bounds(self):
        output = h.AudioOutput()
        output.reset()
        self.assertEqual(h.pygame.mixer.get_init(), (32000, -16, 1))
        for _ in range(100):
            output.feed(bytes(2048))
        self.assertLess(len(output.pending), 8192)
        output.set_volume(2)
        self.assertEqual(output.volume, 1)
        output.stop()
        self.assertFalse(output.channel.get_busy())
        output.reset()
        self.assertFalse(output.error)
        h.pygame.mixer.quit()

    def test_touch_menu_keypad_and_recordings(self):
        # Exercise the actual event routing/rendering without a desktop or SDR.
        frames = [0]
        seen = []
        clicks = {1: (515, 20), 3: (240, 120), 5: (540, 120),
                  7: (50, 120), 9: (50, 110), 11: (50, 180),
                  13: (540, 180), 15: (515, 20), 17: (540, 210),
                  19: (540, 450), 21: (540, 450)}
        def events():
            frames[0] += 1
            if frames[0] in clicks:
                return [h.pygame.event.Event(h.pygame.MOUSEBUTTONDOWN, button=1, pos=clicks[frames[0]])]
            if frames[0] == 23:
                return [h.pygame.event.Event(h.pygame.QUIT)]
            return []
        original_label = h.label
        def label(*args, **kwargs):
            seen.append(str(args[2]))
            return original_label(*args, **kwargs)
        with patch('sys.argv', ['hunter', '--demo', '--windowed']), patch.object(h, 'save_settings'), patch.object(h, 'load_settings', return_value={}), patch.object(h.pygame.event, 'get', side_effect=events), patch.object(h, 'label', side_effect=label):
            h.main()
        self.assertTrue(any('CONTROLS' in x for x in seen))
        self.assertTrue(any('KEYPAD' in x for x in seen))
        self.assertTrue(any('FILES' in x for x in seen))
        self.assertTrue(any('14.000000 MHz' in x for x in seen))


if __name__ == '__main__':
    unittest.main()
