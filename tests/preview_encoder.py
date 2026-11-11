"""Render the dial settings using simulated hardware and isolated preferences."""
from pathlib import Path
from unittest.mock import patch
import test_v060 as tests

h = tests.h
output = Path(__file__).resolve().parents[1] / '.qa'
output.mkdir(exist_ok=True)
frame = [0]
def events():
    frame[0] += 1
    if frame[0] in (1, 3):
        pos = (515,20) if frame[0] == 1 else (280,450)
        return [h.pygame.event.Event(h.pygame.MOUSEBUTTONDOWN, button=1, pos=pos)]
    return [h.pygame.event.Event(h.pygame.QUIT)] if frame[0] == 6 else []

original_flip = h.pygame.display.flip
def capture():
    original_flip()
    if frame[0] == 4:
        h.pygame.image.save(h.pygame.display.get_surface(), str(output/'encoder-v060.png'))

def simulated_start(self, chip=None):
    self.closed = False
    self.status = 'GPIO ready (simulated for preview)'

with patch.object(h, 'SETTINGS_FILE', output/'encoder-preview-settings.json'), patch.object(h.EncoderInput, 'start', simulated_start), patch('sys.argv',['hunter','--demo','--encoder','--windowed','--rotation','0']), patch.object(h.pygame.event,'get',side_effect=events), patch.object(h.pygame.time,'get_ticks',side_effect=lambda:frame[0]*150), patch.object(h.pygame.display,'flip',side_effect=capture):
    h.main()
