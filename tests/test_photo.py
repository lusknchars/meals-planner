"""Contract for fetching one product photo to attach.

The line does not preview links: a Kroger URL arrives as blue text and opens a
thumbnail. The platform does upload local files though, so a photo is fetched
here and attached instead.

Two rules carry the safety of this, and both are tested:

* The URL is BUILT from a product ID, never taken from input. An agent that
  could be told to fetch an arbitrary address would be a way to pull anything
  into somebody's chat.
* The ID must appear in that store's price snapshot, so only products actually
  priced for this person can be fetched.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/photo.py'
sys.path.insert(0, str(SCRIPT.parent))
import photo  # noqa: E402

JPEG = b'\xff\xd8\xff\xe0' + b'stand-in jpeg bytes' * 8
PRICES = {
    'captured': '2026-09-16T04:00:00+00:00', 'source': 'Kroger Products API',
    'licence': 'Kroger developer terms; not redistributed', 'location_id': '70300022',
    'items': {
        'avocado': [
            {'product_id': '0000000004046', 'description': 'Fresh Medium Ripe Avocado',
             'brand': 'Kroger', 'size': '1 each', 'price': 1.5, 'promo': None,
             'unit_price': 1.5, 'unit': 'ct', 'relevant': True,
             'image': 'https://www.kroger.com/product/images/medium/front/0000000004046'},
        ],
    },
}


class Url(unittest.TestCase):
    def test_the_url_is_built_from_the_product_id(self):
        built = photo.image_url('0000000004046', 'xlarge')
        self.assertEqual(built,
                         'https://www.kroger.com/product/images/xlarge/front/0000000004046')

    def test_an_id_that_is_not_digits_is_refused(self):
        for bad in ('../../etc/passwd', 'http://evil.test/x.jpg', '', 'abc123'):
            with self.assertRaises(ValueError, msg=bad):
                photo.image_url(bad, 'xlarge')

    def test_an_unknown_size_is_refused(self):
        with self.assertRaises(ValueError):
            photo.image_url('0000000004046', 'enormous')


class Fetching(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.prices_dir = self.home / 'snapshots'
        self.prices_dir.mkdir()
        (self.prices_dir / 'prices.70300022.json').write_text(json.dumps(PRICES))
        self.calls = []

    def fetch(self, payload=JPEG, status=200):
        def caller(method, url, headers=None, timeout=None):
            self.calls.append(url)
            return status, payload
        return caller

    def grab(self, product_id='0000000004046', size='xlarge', fetch=None, payload=JPEG):
        return photo.fetch_photo(fetch or self.fetch(payload), '70300022', product_id,
                                 size=size, home=self.home, prices_dir=self.prices_dir)

    def test_a_priced_product_is_written_and_described(self):
        found = self.grab()
        path = Path(found['path'])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), JPEG)
        self.assertEqual(found['product'], 'Fresh Medium Ripe Avocado')
        self.assertEqual(found['brand'], 'Kroger')
        self.assertEqual(found['bytes'], len(JPEG))
        self.assertIn('cache', str(path), 'the Hermes cache is trusted for delivery')
        self.assertIn('xlarge', self.calls[0])

    def test_a_product_not_in_the_snapshot_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.grab(product_id='9999999999999')
        self.assertIn('not in', str(caught.exception).lower())
        self.assertEqual(self.calls, [], 'nothing should be fetched for an unknown product')

    def test_something_that_is_not_a_jpeg_is_refused_and_not_kept(self):
        with self.assertRaises(ValueError) as caught:
            self.grab(payload=b'<!doctype html><html>not an image</html>')
        self.assertIn('jpeg', str(caught.exception).lower())
        written = list((self.home / 'cache').rglob('*.jpg')) if (self.home / 'cache').is_dir() else []
        self.assertEqual(written, [], 'a rejected download leaves nothing behind')

    def test_an_oversized_response_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.grab(payload=b'\xff\xd8\xff' + b'x' * (photo.MAX_BYTES + 1))
        self.assertIn('too large', str(caught.exception).lower())

    def test_a_photo_already_on_disk_is_not_fetched_again(self):
        first = self.grab()
        self.calls.clear()
        second = self.grab()
        self.assertEqual(first['path'], second['path'])
        self.assertEqual(self.calls, [], 'a cached photo costs no request')
        self.assertTrue(second['cached'])

    def test_a_refused_download_reports_the_status(self):
        with self.assertRaises(ValueError) as caught:
            self.grab(fetch=self.fetch(payload=b'nope', status=404))
        self.assertIn('404', str(caught.exception))


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.prices_dir = self.home / 'snapshots'
        self.prices_dir.mkdir()
        (self.prices_dir / 'prices.70300022.json').write_text(json.dumps(PRICES))

    def test_an_unknown_product_exits_nonzero_with_json_on_stderr(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--store', '70300022',
             '--product-id', '9999999999999'],
            env={**os.environ, 'HERMES_HOME': str(self.home),
                 'MEALS_PRICES_DIR': str(self.prices_dir)},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('error', json.loads(result.stderr))


if __name__ == '__main__':
    unittest.main()
