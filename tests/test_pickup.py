"""Contract for collecting the shopping.

A price snapshot used to know only a location id, so the agent could price a
basket at a shop it could not name, place, or give the closing time of. The
store record now travels with the prices.

Delivery cost is absent on purpose. Kroger's product API publishes prices and
stores, not fulfilment fees, and a delivery charge guessed at is the exact kind
of number this agent exists not to give.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'

STORE = {'location_id': '70300051', 'name': 'Ralphs Fresh Fare - Hillcrest',
         'chain': 'RALPHS', 'address': '1020 University Ave', 'city': 'San Diego',
         'state': 'CA', 'zip': '92103', 'phone': '6192982931',
         'lat': 32.749267, 'lon': -117.154764, 'timezone': 'America/Los_Angeles',
         'hours': {day: {'open': '06:00', 'close': '23:00', 'open24': False}
                   for day in ('monday', 'tuesday', 'wednesday', 'thursday',
                               'friday', 'saturday', 'sunday')}}
CATALOGUE = {'currency': 'USD', 'venues': [], 'categories': {'oats': 'Pantry'},
             'recipes': [{'id': 'a', 'title': 'Oats', 'slot': 'breakfast', 'calories': 400,
                          'cost': 1.0, 'tags': [], 'ingredients': [
                              {'item': 'oats', 'quantity': 80, 'unit': 'g', 'cost': 1.0}]}]}


class Pickup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))

    def snapshot(self, store=STORE):
        payload = {'captured': '2026-09-16T19:00:00+00:00', 'location_id': '70300051',
                   'source': 'Kroger Products API', 'items': {}}
        if store is not None:
            payload['store'] = store
        (self.home / 'prices.70300051.json').write_text(json.dumps(payload))

    def call(self, *args, success=True):
        env = {**os.environ, 'HERMES_HOME': str(self.home),
               'MEALS_CATALOGUE': str(self.catalogue),
               'MEALS_PRICES_DIR': str(self.home)}
        run = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *args],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0 if success else 1, run.stderr)
        return json.loads(run.stdout if success else run.stderr)

    def profile(self, *extra):
        self.call('profile', 'set', '--people', '1', '--calories', '2000',
                  '--store', '70300051', *extra)

    def test_it_says_where_the_shop_is(self):
        self.snapshot(); self.profile()
        found = self.call('pickup')
        self.assertEqual(found['store']['name'], 'Ralphs Fresh Fare - Hillcrest')
        self.assertEqual(found['store']['address'], '1020 University Ave')
        self.assertEqual(found['store']['phone'], '6192982931')

    def test_it_measures_the_distance_when_they_have_said_where_they_are(self):
        self.snapshot(); self.profile('--lat', '32.7157', '--lon', '-117.1611')
        found = self.call('pickup')
        self.assertAlmostEqual(found['distance_km'], 3.8, delta=0.6)
        self.assertIsNone(found['distance_note'])

    def test_an_unknown_distance_is_said_not_guessed(self):
        self.snapshot(); self.profile()
        found = self.call('pickup')
        self.assertIsNone(found['distance_km'], 'no location means no distance, not zero')
        self.assertIn('postcode', found['distance_note'])

    def test_it_gives_today_s_hours(self):
        self.snapshot(); self.profile()
        found = self.call('pickup')
        self.assertEqual(found['open_today']['close'], '23:00')
        self.assertIn(found['day'], ('monday', 'tuesday', 'wednesday', 'thursday',
                                     'friday', 'saturday', 'sunday'))

    def test_it_never_invents_a_delivery_price(self):
        self.snapshot(); self.profile()
        found = self.call('pickup')
        self.assertIn('no delivery price', found['delivery'])
        self.assertNotIn('$', found['delivery'])

    def test_an_older_snapshot_says_so_instead_of_failing(self):
        self.snapshot(store=None); self.profile()
        found = self.call('pickup')
        self.assertIsNone(found['store'])
        self.assertIn('rebuild it', found['note'])
        self.assertIn('no delivery price', found['delivery'])

    def test_without_a_snapshot_it_says_what_to_do(self):
        self.profile()
        failed = self.call('pickup', success=False)
        self.assertIn('compare.py', failed['error'])


if __name__ == '__main__':
    unittest.main()
