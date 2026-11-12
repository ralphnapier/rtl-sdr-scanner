"""Check Linux diagnostics without requiring radio or display packages on Windows."""
import ast
import tempfile
import time
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / 'rf_hunter_v062_diagnostics.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
diagnostics_node = next(node for node in tree.body
                        if isinstance(node, ast.ClassDef) and node.name == 'SystemDiagnostics')
module = ast.fix_missing_locations(ast.Module(body=[diagnostics_node], type_ignores=[]))
namespace = {'Path': Path, 'time': time}
exec(compile(module, str(SOURCE), 'exec'), namespace)
SystemDiagnostics = namespace['SystemDiagnostics']


class SystemDiagnosticsTests(unittest.TestCase):
    def test_cpu_ram_temperature_and_one_second_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc, thermal = root / 'proc', root / 'thermal'
            proc.mkdir()
            thermal.mkdir()
            (proc / 'stat').write_text('cpu  100 0 100 800 0 0 0 0\n')
            (proc / 'meminfo').write_text('MemTotal: 1000000 kB\nMemAvailable: 250000 kB\n')
            for zone, name, temp in [('thermal_zone0', 'cpu-thermal', '51000'),
                                     ('thermal_zone1', 'rp1-thermal', '43000')]:
                sensor = thermal / zone
                sensor.mkdir()
                (sensor / 'type').write_text(name)
                (sensor / 'temp').write_text(temp)
            readings = SystemDiagnostics(proc, thermal)
            readings.sample(now=10)
            self.assertIsNone(readings.cpu_percent)  # Needs two counter snapshots.
            self.assertEqual(readings.memory, (750000 * 1024, 1000000 * 1024))
            self.assertEqual(readings.temperatures,
                             (('cpu-thermal', 51.0), ('rp1-thermal', 43.0)))

            (proc / 'stat').write_text('cpu  150 0 150 900 0 0 0 0\n')
            readings.sample(now=10.5)
            self.assertIsNone(readings.cpu_percent)  # Cached for a full second.
            readings.sample(now=11)
            self.assertAlmostEqual(readings.cpu_percent, 50.)

    def test_missing_sensors_leave_overlay_data_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('proc').mkdir()
            root.joinpath('thermal').mkdir()
            readings = SystemDiagnostics(root / 'proc', root / 'thermal')
            readings.sample(now=1)
            self.assertIsNone(readings.cpu_percent)
            self.assertIsNone(readings.memory)
            self.assertEqual(readings.temperatures, ())


if __name__ == '__main__':
    unittest.main()
