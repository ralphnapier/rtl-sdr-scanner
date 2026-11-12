"""Saved-frequency and online matching tests without radio/display packages."""
import ast
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'rf_hunter_v063_frequency_names.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
         and node.name in ('FrequencyNames', '_NoApiRedirect', 'fetch_open_repeater')]
module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
namespace = {'Path': Path, 'json': json, 'urllib': urllib,
             'FREQUENCIES_FILE': Path('frequencies.json'),
             'REPEATER_LAT': 38.9914, 'REPEATER_LON': -84.6461,
             'REPEATER_RADIUS_KM': 50}
exec(compile(module, str(SOURCE), 'exec'), namespace)
FrequencyNames = namespace['FrequencyNames']
fetch_open_repeater = namespace['fetch_open_repeater']


class FrequencyNamesTests(unittest.TestCase):
    def test_manual_names_override_online_and_ambiguous_channels_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'frequencies.json'
            names = FrequencyNames(path)
            names.add(146_940_000)
            names.add(146_970_000)
            updated, ambiguous = names.merge_online([
                {'frequency': 146.94, 'callsign': 'K4ONE', 'city': 'Florence',
                 'mode': 'FM', 'status': 'active'},
                {'frequency': 146.97, 'callsign': 'K4TWO', 'city': 'Covington',
                 'mode': 'FM', 'status': 'active'},
                {'frequency': 146.97, 'callsign': 'K4THREE', 'city': 'Cincinnati',
                 'mode': 'FM', 'status': 'active'},
                {'frequency': 146.94, 'callsign': 'OLD', 'city': 'Florence',
                 'mode': 'FM', 'status': 'inactive'}])
            self.assertEqual((updated, ambiguous), (1, 1))
            self.assertEqual(names.label_for(146_942_000), 'K4ONE Florence')
            self.assertEqual(names.label_for(146_970_000), '')
            names.rename(146_940_000, 'My local repeater')
            self.assertEqual(names.label_for(146_940_000), 'My local repeater')
            self.assertEqual(FrequencyNames(path).label_for(146_940_000),
                             'My local repeater')
            names.clear_manual(146_940_000)
            self.assertEqual(names.label_for(146_940_000), 'K4ONE Florence')

    def test_online_request_uses_header_and_nearby_search(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _size):
                return json.dumps([{'frequency': 146.94, 'callsign': 'K4ONE'}]).encode()
        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()
        opener = Opener()
        with patch.object(urllib.request, 'build_opener', return_value=opener):
            rows, truncated = fetch_open_repeater('or_test_key')
        self.assertEqual(len(rows), 1)
        self.assertFalse(truncated)
        self.assertEqual(opener.request.headers['X-api-key'], 'or_test_key')
        self.assertIn('lat=38.9914', opener.request.full_url)
        self.assertIn('radius=50', opener.request.full_url)
        self.assertEqual(opener.timeout, 10)

    def test_key_is_required_before_network_access(self):
        with self.assertRaises(ValueError):
            fetch_open_repeater('')


if __name__ == '__main__':
    unittest.main()
