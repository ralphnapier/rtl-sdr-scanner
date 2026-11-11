"""Render the touchscreen library using synthetic WAVs, not user recordings."""
import json
from pathlib import Path
import time
import test_v058 as tests

h = tests.h
output = Path(__file__).resolve().parents[1] / '.qa'
directory = output / 'synthetic-recordings'
directory.mkdir(parents=True, exist_ok=True)
for i, (frequency, modulation, peak, average) in enumerate([
        (125.0125, 'AM', 29.4, 18.2), (162.55, 'NFM', 22.1, 14.3),
        (128.7, 'AM', 15.8, 9.2), (146.52, 'NFM', None, None)]):
    path = directory / f'RF_20261111_12000{i}_{frequency:.3f}MHz_001_{modulation}.wav'
    tests.make_wav(path, i+1)
    if peak is not None:
        path.with_suffix('.json').write_text(json.dumps({
            'schema_version': 1, 'complete': True, 'frequency_hz': frequency*1e6,
            'modulation': modulation, 'peak_signal_db': peak, 'mean_signal_db': average,
            'signal_units': 'dB above local noise'}))
h.pygame.init()
screen = h.pygame.display.set_mode((640, 480))
audio = h.AudioOutput()
audio.reset()
browser = h.RecordingBrowser(screen, audio, directory)
deadline = time.monotonic()+5
while browser.loading and time.monotonic()<deadline:
    browser.draw()
    time.sleep(.01)
h.pygame.image.save(screen, str(output / 'recordings-list.png'))
browser.action('select:0')
browser.draw()
h.pygame.image.save(screen, str(output / 'recording-details.png'))
browser.action('back')
browser.action('freq')
browser.draw()
h.pygame.image.save(screen, str(output / 'recording-filter.png'))
browser.stop()
h.pygame.quit()
