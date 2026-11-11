import importlib.util
from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import patch
import test_v057

spec = importlib.util.spec_from_file_location('hunter059', Path(__file__).resolve().parents[1] / 'rf_hunter_v059_display_rotation.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class AudioRegressionTests(test_v057.AudioTests):
    def setUp(self):
        self.patcher = patch.object(test_v057, 'h', h)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()


class RotationTests(unittest.TestCase):
    def tearDown(self):
        h.pygame.quit()

    def test_render_and_input_are_inverse_at_corners_and_center(self):
        h.pygame.init()
        for angle in (0, 180):
            display = h.RotatingDisplay(True, angle)
            display.surface.fill((0, 0, 0))
            for point in ((0, 0), (639, 479), (320, 240), (50, 100)):
                display.surface.set_at(point, (255, 10, 20))
                display.present()
                physical = display.point(*point)
                self.assertEqual(display.point(*physical), point)
                self.assertEqual(tuple(display.output.get_at(physical))[:3], (255, 10, 20))
            self.assertEqual(display.point(640, 480), display.point(639, 479))

    def test_persists_and_preserves_other_settings(self):
        h.pygame.init()
        with tempfile.TemporaryDirectory() as directory, patch.object(h, 'SETTINGS_FILE', Path(directory)/'settings.json'):
            h.save_settings(19.7, 12, 2)
            display = h.RotatingDisplay(True)
            display.toggle()
            h.save_settings(20.7, 15, 3)
            self.assertEqual(h.load_settings()['rotation'], 180)
            self.assertEqual(h.load_settings()['gain_db'], 20.7)
            self.assertEqual(h.RotatingDisplay(True).rotation, 180)
            self.assertEqual(h.RotatingDisplay(True, 0).rotation, 0)

    def test_rotated_touch_opens_recordings_then_rotates_back(self):
        frames = [0]
        # MENU, rotation toggle; subsequent points are physical rotated coordinates.
        clicks = {1:(515,20), 3:(80,450), 5:(99,269), 7:(99,459), 9:(540,450)}
        seen = []
        def events():
            frames[0] += 1
            if frames[0] == 5:
                x, y = clicks[5]
                return [h.pygame.event.Event(h.pygame.FINGERDOWN, x=x/640, y=y/480),
                        h.pygame.event.Event(h.pygame.MOUSEBUTTONDOWN, button=1, pos=(x,y))]
            if frames[0] in clicks:
                return [h.pygame.event.Event(h.pygame.MOUSEBUTTONDOWN, button=1, pos=clicks[frames[0]])]
            if frames[0] == 11:
                return [h.pygame.event.Event(h.pygame.QUIT)]
            return []
        original = h.label
        def label(*args, **kwargs):
            seen.append(str(args[2]))
            return original(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.object(h, 'SETTINGS_FILE', Path(directory)/'settings.json'), patch('sys.argv', ['hunter','--demo','--windowed']), patch.object(h.pygame.event, 'get', side_effect=events), patch.object(h.pygame.time, 'get_ticks', side_effect=lambda: frames[0]*150), patch.object(h, 'label', side_effect=label):
            h.main()
            self.assertEqual(h.load_settings()['rotation'], 0)
        self.assertIn('RF HUNTER / RECORDINGS', seen)


if __name__ == '__main__':
    unittest.main()
