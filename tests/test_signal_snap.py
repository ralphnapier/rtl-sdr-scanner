"""Exercise the pure snapping function without requiring GUI/radio packages."""
import ast
import math
from pathlib import Path
import unittest

source = Path(__file__).resolve().parents[1] / 'rf_hunter_v060_encoder.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == 'snap_to_signal')
namespace = {'math': math}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
snap = namespace['snap_to_signal']


class SignalSnapTests(unittest.TestCase):
    def setUp(self):
        self.freqs = list(range(100_000, 201_000, 1000))
        self.levels = [-70.] * len(self.freqs)

    def peak(self, hz, db):
        self.levels[self.freqs.index(hz)] = db

    def test_near_miss_snaps(self):
        self.peak(158_000, -45)
        self.assertEqual(snap(150_000, self.freqs, self.levels), 158_000)

    def test_distant_stronger_peak_is_ignored(self):
        self.peak(170_000, -10)
        self.peak(158_000, -45)
        self.assertEqual(snap(150_000, self.freqs, self.levels), 158_000)

    def test_no_strong_local_peak(self):
        self.peak(158_000, -63)
        self.assertEqual(snap(150_000, self.freqs, self.levels), 150_000)

    def test_already_on_strong_signal(self):
        self.peak(150_000, -45)
        self.peak(158_000, -40)
        self.assertEqual(snap(150_000, self.freqs, self.levels), 150_000)

    def test_missing_or_coarse_data(self):
        self.assertEqual(snap(150_000, None, None), 150_000)
        self.assertEqual(snap(150_000, self.freqs[::10], self.levels[::10]), 150_000)

    def test_does_not_cross_dc_gap(self):
        self.peak(158_000, -40)
        pairs = [(f, p) for f, p in zip(self.freqs, self.levels)
                 if not 151_000 <= f <= 157_000]
        f, p = zip(*pairs)
        self.assertEqual(snap(150_000, f, p), 150_000)

    def test_hard_distance_limit(self):
        self.peak(163_000, -10)
        self.assertEqual(snap(150_000, self.freqs, self.levels), 150_000)


if __name__ == '__main__':
    unittest.main()
