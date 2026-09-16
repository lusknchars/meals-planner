"""Contract for the skill reading refresh.py's snapshots.

Real store prices replace catalogue estimates when a snapshot covers the
ingredient, and the reply must say which figure it used. A value label is only
ever computed from prices actually in the snapshot — never guessed.
"""
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
        {'id': 'oats', 'title': 'Oats', 'slot': 'breakfast', 'calories': 420, 'cost': 2.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'oats', 'quantity': 100, 'unit': 'g', 'cost': 2.0}]},
        {'id': 'rice-beans', 'title': 'Rice and beans', 'slot': 'lunch', 'calories': 650,
         'cost': 3.0, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'rice', 'quantity': 100, 'unit': 'g', 'cost': 1.5},
                         {'item': 'beans', 'quantity': 100, 'unit': 'g', 'cost': 1.5}]},
        {'id': 'tofu', 'title': 'Tofu', 'slot': 'dinner', 'calories': 700, 'cost': 4.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'tofu', 'quantity': 200, 'unit': 'g', 'cost': 4.0}]},
    ],
    'venues': [],
}

STORES = {
    'captured': '2026-09-16T01:00:00+00:00',
    'source': 'OpenStreetMap via Overpass',
    'licence': 'ODbL 1.0',
    'stores': [
        {'id': 'osm:node/1', 'name': 'Ralphs Wilshire', 'brand': 'Ralphs', 'shop': 'supermarket',
         'address': '1233 Wilshire Blvd', 'city': 'Los Angeles',
         'hours': 'Mo-Su 06:00-23:00', 'lat': 34.0532, 'lon': -118.2637, 'distance_km': 1.8},
        {'id': 'osm:node/2', 'name': 'Whole Foods Market', 'brand': 'Whole Foods Market',
         'shop': 'supermarket', 'address': '788 W 8th St', 'city': 'Los Angeles',
         'hours': None, 'lat': 34.0490, 'lon': -118.2600, 'distance_km': 0.9},
    ],
}

PRICES = {
    'captured': '2026-09-16T01:05:00+00:00',
    'source': 'Kroger Products API',
    'licence': 'Kroger developer terms; not redistributed',
    'location_id': '70100123',
    'items': {
        'oats': [
            {'product_id': 'p1', 'description': 'Kroger Rolled Oats', 'brand': 'Kroger',
             'size': '32 oz', 'price': 4.49, 'promo': None, 'unit_price': 4.95,
             'unit': 'kg', 'image': None},
            {'product_id': 'p2', 'description': 'Organic Steel Cut Oats',
             'brand': 'Simple Truth', 'size': '24 oz', 'price': 7.99, 'promo': None,
             'unit_price': 11.74, 'unit': 'kg', 'image': None},
        ],
        'rice': [
            {'product_id': 'p3', 'description': 'Long Grain Rice', 'size': 'family pack',
             'price': 8.99, 'promo': None, 'unit_price': None, 'unit': None, 'image': None},
        ],
        'beans': [
            {'product_id': 'p4', 'description': 'Black Beans', 'size': '2 lb', 'price': 3.29,
             'promo': 2.99, 'unit_price': 3.63, 'unit': 'kg',
             'image': 'https://www.kroger.com/product/images/medium/front/p4'},
        ],
    },
}


class SnapshotAware(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))
        self.stores = self.home / 'stores.json'
        self.stores.write_text(json.dumps(STORES))
        self.prices_dir = self.home / 'snapshots'
        self.prices_dir.mkdir()
        (self.prices_dir / 'prices.70100123.json').write_text(json.dumps(PRICES))

    def call(self, *args, scope='chat-1', success=True, prices=True, stores=True,
             stores_override=None):
        env = {**os.environ, 'HERMES_HOME': str(self.home), 'MEALS_CATALOGUE': str(self.catalogue)}
        if stores:
            env['MEALS_STORES'] = str(self.stores)
        elif stores_override:
            env['MEALS_STORES'] = stores_override
        if prices:
            env['MEALS_PRICES_DIR'] = str(self.prices_dir)
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', scope, *args],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def profile(self, **overrides):
        args = {'--people': '1', '--calories': '2000', '--budget': '100',
                '--diet': 'vegetarian', '--lat': '34.0522', '--lon': '-118.2437',
                '--store': '70100123'}
        args.update(overrides)
        flat = [value for pair in args.items() for value in pair]
        return self.call('profile', 'set', *flat)['profile']

    # Stores

    def test_stores_command_lists_the_nearest_shops(self):
        self.profile()
        found = self.call('stores', '--limit', '2')
        self.assertEqual([store['name'] for store in found['stores']],
                         ['Whole Foods Market', 'Ralphs Wilshire'])
        self.assertLess(found['stores'][0]['distance_km'], found['stores'][1]['distance_km'])
        self.assertEqual(found['source'], 'OpenStreetMap via Overpass')
        self.assertEqual(found['licence'], 'ODbL 1.0')

    def test_stores_without_a_snapshot_says_so(self):
        self.profile()
        # Point MEALS_STORES at a path that does not exist, rather than unsetting it:
        # unset falls back to the directory beside the catalogue, which in a real
        # checkout holds the shipped snapshot, so the test would pass on the wrong file.
        missing = self.home / 'absent' / 'stores.json'
        error = self.call('stores', '--limit', '2', success=False,
                          stores=False, stores_override=str(missing))
        self.assertIn('snapshot', error['error'].lower())

    # Prices in the shopping list

    def test_shopping_prefers_snapshot_prices_and_names_the_source(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        shopping = self.call('shopping', '--start', '2026-09-16')
        by_item = {item['item']: item for item in shopping['items']}
        oats = by_item['oats']
        # 100 g of the cheapest oats at 4.95/kg, not the catalogue's flat 2.00.
        # 4.95 * 100 * 0.001 lands just under 0.495 in binary, so it rounds down.
        self.assertEqual(oats['cost'], 0.49)
        self.assertEqual(oats['price_source'], 'kroger:70100123 2026-09-16')
        self.assertEqual(oats['product'], 'Kroger Rolled Oats')
        self.assertEqual(oats['brand'], 'Kroger')
        self.assertEqual(oats['value'], 'best value')

    def test_a_line_without_a_brand_says_nothing_rather_than_none(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        shopping = self.call('shopping', '--start', '2026-09-16')
        # beans' fixture row carries no brand: the key exists and is null, never
        # the string "None" leaking into a message someone reads on a phone.
        beans = {item['item']: item for item in shopping['items']}['beans']
        self.assertIn('brand', beans)
        self.assertIsNone(beans['brand'])

    def test_unparseable_size_keeps_the_estimate_and_admits_it(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        shopping = self.call('shopping', '--start', '2026-09-16')
        rice = {item['item']: item for item in shopping['items']}['rice']
        self.assertEqual(rice['cost'], 1.5, 'no unit price, so the catalogue estimate stands')
        self.assertEqual(rice['price_source'], 'catalogue estimate')
        self.assertEqual(rice['package_price'], 8.99)
        self.assertIsNone(rice['value'])

    def test_promo_price_wins_and_totals_say_what_they_mixed(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        shopping = self.call('shopping', '--start', '2026-09-16')
        beans = {item['item']: item for item in shopping['items']}['beans']
        # 100 g of 2 lb at the 2.99 promo: 2.99 / 0.907 kg = 3.30/kg -> 0.33
        self.assertEqual(beans['cost'], 0.33)
        self.assertEqual(beans['promo'], 2.99)
        self.assertEqual(shopping['priced_from_snapshot'], 2)
        # rice ("family pack" has no per-kilo price) and tofu (absent from the
        # snapshot entirely) both fall back to the catalogue.
        self.assertEqual(shopping['estimated'], 2)

    def test_without_a_price_snapshot_nothing_changes(self):
        self.profile()
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        shopping = self.call('shopping', '--start', '2026-09-16', prices=False)
        costs = {item['item']: item['cost'] for item in shopping['items']}
        self.assertEqual(costs, {'oats': 2.0, 'rice': 1.5, 'beans': 1.5, 'tofu': 4.0})
        self.assertTrue(all(item['price_source'] == 'catalogue estimate'
                            for item in shopping['items']))
        self.assertEqual(shopping['priced_from_snapshot'], 0)


class OffTargetProducts(unittest.TestCase):
    """A count-priced product that is not the ingredient must never win the line.
    Live, a plantain at $0.99 each priced a week of bananas at $7.92."""

    CATALOGUE = {
        'currency': 'USD',
        # plan needs every slot filled, so lunch and dinner are here as ballast:
        # only the banana line is under test.
        'recipes': [{'id': 'fruit', 'title': 'Fruit plate', 'slot': 'breakfast',
                     'calories': 300, 'cost': 1.0, 'tags': ['vegetarian'],
                     'ingredients': [{'item': 'banana', 'quantity': 4, 'unit': 'un',
                                      'cost': 1.0}]},
                    {'id': 'salad', 'title': 'Salad', 'slot': 'lunch',
                     'calories': 600, 'cost': 2.0, 'tags': ['vegetarian'],
                     'ingredients': [{'item': 'lettuce', 'quantity': 200, 'unit': 'g',
                                      'cost': 2.0}]},
                    {'id': 'soup', 'title': 'Soup', 'slot': 'dinner',
                     'calories': 700, 'cost': 3.0, 'tags': ['vegetarian'],
                     'ingredients': [{'item': 'lentils', 'quantity': 150, 'unit': 'g',
                                      'cost': 3.0}]}],
        'venues': [],
    }
    PRICES = {
        'captured': '2026-09-16T02:00:00+00:00', 'source': 'Kroger Products API',
        'licence': 'Kroger developer terms; not redistributed',
        'location_id': '70300022',
        'items': {'banana': [
            {'product_id': 'p9', 'description': 'Fresh Plantain - Single', 'size': '1 ea',
             'price': 0.99, 'promo': None, 'unit_price': 0.99, 'unit': 'ct',
             'relevant': False, 'image': None},
            {'product_id': 'p8', 'description': 'Fresh Bunch of Bananas', 'size': '1 lb',
             'price': 0.69, 'promo': None, 'unit_price': 1.52, 'unit': 'kg',
             'relevant': True, 'image': None},
        ]},
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(self.CATALOGUE))
        self.prices_dir = self.home / 'snapshots'
        self.prices_dir.mkdir()
        (self.prices_dir / 'prices.70300022.json').write_text(json.dumps(self.PRICES))

    def call(self, *args, success=True):
        env = {**os.environ, 'HERMES_HOME': str(self.home),
               'MEALS_CATALOGUE': str(self.catalogue),
               'MEALS_PRICES_DIR': str(self.prices_dir)}
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *args],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def test_an_off_target_count_product_never_prices_the_line(self):
        self.call('profile', 'set', '--people', '1', '--calories', '2000',
                  '--diet', 'vegetarian', '--store', '70300022')
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        shopping = self.call('shopping', '--start', '2026-09-16')
        banana = {item['item']: item for item in shopping['items']}['banana']
        # The only comparable row is the plantain, and it is not a banana: the
        # bunch is priced by weight, which a count recipe cannot use. So the
        # catalogue estimate stands, and the reply says so.
        self.assertEqual(banana['cost'], 1.0)
        self.assertEqual(banana['price_source'], 'catalogue estimate')
        self.assertIsNone(banana['product'])
        self.assertEqual(shopping['priced_from_snapshot'], 0)


if __name__ == '__main__':
    unittest.main()
