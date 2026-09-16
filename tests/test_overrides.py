"""Contract for prices the owner confirms themselves.

Ralphs prices bananas by weight; the recipe counts them. No free API publishes a
per-piece produce price, so the only honest source is a person confirming one —
and a confirmed price is only worth trusting while it is fresh. Every override
carries where it came from and when, and expires, so a total can always name the
origin of every number in it.
"""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'

CATALOGUE = {
    'currency': 'USD',
    'recipes': [
        {'id': 'fruit', 'title': 'Fruit plate', 'slot': 'breakfast', 'calories': 300,
         'cost': 1.0, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'banana', 'quantity': 2, 'unit': 'un', 'cost': 1.0}]},
        {'id': 'bowl', 'title': 'Rice bowl', 'slot': 'lunch', 'calories': 600, 'cost': 2.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'rice', 'quantity': 100, 'unit': 'g', 'cost': 2.0}]},
        {'id': 'soup', 'title': 'Soup', 'slot': 'dinner', 'calories': 700, 'cost': 3.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'lentils', 'quantity': 150, 'unit': 'g', 'cost': 3.0}]},
    ],
    'venues': [],
}

PRICES = {
    'captured': '2026-09-16T02:00:00+00:00',
    'source': 'Kroger Products API',
    'licence': 'Kroger developer terms; not redistributed',
    'location_id': '70300022',
    'items': {
        'banana': [
            {'product_id': 'b1', 'description': 'Fresh Bunch of Bananas', 'brand': None,
             'size': '1 lb', 'price': 0.69, 'promo': None, 'unit_price': 1.52,
             'unit': 'kg', 'relevant': True, 'image': None},
        ],
        'rice': [
            {'product_id': 'r1', 'description': 'Kroger Long Grain Rice', 'brand': 'Kroger',
             'size': '32 oz', 'price': 1.99, 'promo': None, 'unit_price': 2.19,
             'unit': 'kg', 'relevant': True, 'image': None},
        ],
    },
}


class Overrides(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))
        self.prices_dir = self.home / 'snapshots'
        self.prices_dir.mkdir()
        (self.prices_dir / 'prices.70300022.json').write_text(json.dumps(PRICES))

    def call(self, *args, success=True, days=None):
        env = {**os.environ, 'HERMES_HOME': str(self.home),
               'MEALS_CATALOGUE': str(self.catalogue),
               'MEALS_PRICES_DIR': str(self.prices_dir)}
        if days is not None:
            env['MEALS_OVERRIDE_DAYS'] = str(days)
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *args],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def profile(self):
        return self.call('profile', 'set', '--people', '1', '--calories', '2000',
                         '--diet', 'vegetarian', '--store', '70300022')

    def basket(self, **kwargs):
        shopping = self.call('shopping', '--start', '2026-09-16', **kwargs)
        return {item['item']: item for item in shopping['items']}, shopping

    def test_an_override_records_its_source_and_date(self):
        self.profile()
        saved = self.call('override', 'set', '--item', 'banana', '--price', '0.22',
                          '--unit', 'un', '--source', "Trader Joe's, seen on the web")['override']
        self.assertEqual(saved['item'], 'banana')
        self.assertEqual(saved['price'], 0.22)
        self.assertEqual(saved['unit'], 'un')
        self.assertEqual(saved['source'], "Trader Joe's, seen on the web")
        self.assertRegex(saved['captured'], r'^\d{4}-\d{2}-\d{2}')
        self.assertEqual(self.call('override', 'list')['overrides'][0]['item'], 'banana')

    def test_a_confirmed_price_prices_the_line_and_names_who_said_so(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        self.call('override', 'set', '--item', 'banana', '--price', '0.22', '--unit', 'un',
                  '--source', "Trader Joe's")
        items, shopping = self.basket()
        banana = items['banana']
        self.assertEqual(banana['cost'], 0.44)
        self.assertTrue(banana['price_source'].startswith('you confirmed'), banana['price_source'])
        self.assertIn("Trader Joe's", banana['price_source'])
        self.assertEqual(banana['unit_price'], 0.22)
        self.assertIsNone(banana['value'], 'a confirmed price has no local spread to rank against')
        self.assertEqual(shopping['confirmed_by_you'], 1)

    def test_a_confirmed_price_beats_the_snapshot_for_that_ingredient(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        self.call('override', 'set', '--item', 'rice', '--price', '3.00', '--unit', 'g',
                  '--source', 'the corner shop')
        items, _ = self.basket()
        # Deliberately dearer than the snapshot's $2.19/kg: an explicit instruction
        # wins over a lookup, because the person is standing in the shop.
        self.assertEqual(items['rice']['cost'], 300.0)
        self.assertTrue(items['rice']['price_source'].startswith('you confirmed'))

    def test_a_stale_override_is_refused_and_reported(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        self.call('override', 'set', '--item', 'banana', '--price', '0.22', '--unit', 'un',
                  '--source', "Trader Joe's", '--date', old)
        items, shopping = self.basket()
        self.assertEqual(items['banana']['price_source'], 'catalogue estimate')
        self.assertEqual(items['banana']['cost'], 1.0)
        self.assertEqual(shopping['stale_overrides'], ['banana'])
        self.assertEqual(shopping['confirmed_by_you'], 0)

    def test_the_freshness_window_is_configurable(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        recent = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        self.call('override', 'set', '--item', 'banana', '--price', '0.22', '--unit', 'un',
                  '--source', "Trader Joe's", '--date', recent)
        items, _ = self.basket(days=30)
        self.assertEqual(items['banana']['cost'], 0.44)
        items, shopping = self.basket(days=5)
        self.assertEqual(items['banana']['price_source'], 'catalogue estimate')
        self.assertEqual(shopping['stale_overrides'], ['banana'])

    def test_an_override_in_the_wrong_unit_is_never_quietly_applied(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        # A per-kilo price cannot pay for a recipe that counts bananas.
        self.call('override', 'set', '--item', 'banana', '--price', '1.52', '--unit', 'kg',
                  '--source', 'Ralphs')
        items, shopping = self.basket()
        self.assertEqual(items['banana']['price_source'], 'catalogue estimate')
        self.assertEqual(shopping['mismatched_overrides'], ['banana'])

    def test_clearing_an_override_returns_the_line_to_its_other_source(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        self.call('override', 'set', '--item', 'rice', '--price', '3.00', '--unit', 'g',
                  '--source', 'the corner shop')
        self.call('override', 'clear', '--item', 'rice')
        items, _ = self.basket()
        self.assertTrue(items['rice']['price_source'].startswith('kroger:70300022'))
        self.assertEqual(self.call('override', 'list')['overrides'], [])

    def test_each_conversation_keeps_its_own_confirmed_prices(self):
        self.profile()
        self.call('override', 'set', '--item', 'banana', '--price', '0.22', '--unit', 'un',
                  '--source', "Trader Joe's")
        other = subprocess.run(
            [sys.executable, str(SCRIPT), '--scope', 'chat-2', 'override', 'list'],
            env={**os.environ, 'HERMES_HOME': str(self.home),
                 'MEALS_CATALOGUE': str(self.catalogue)},
            capture_output=True, text=True)
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertEqual(json.loads(other.stdout)['overrides'], [])


if __name__ == '__main__':
    unittest.main()
