"""Contract for choosing which product a shopping line buys.

The line used to take the cheapest price per kilo. For 380 g of chicken that was
an 8 lb family pack: the best rate on the shelf, and nearly four times the money
at the till for a week that eats a sixth of it. Once the list fills a real cart,
that pick is a purchase, so the product is now the one that costs least for the
amount the week needs.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'


def chicken(product_id, description, size, price, unit_price, promo=None, sold_by='UNIT'):
    return {'product_id': product_id, 'description': description, 'brand': 'Kroger',
            'size': size, 'sold_by': sold_by, 'price': price, 'promo': promo,
            'unit_price': unit_price, 'unit': 'kg', 'relevant': True, 'image': None}


FAMILY = chicken('family', 'Chicken Breast Family Pack', '8 lb', 17.52, 4.83)
TRAY = chicken('tray', 'Chicken Breast Tray', '1.5 lb', 9.99, 14.68)
COUNTER = chicken('counter', 'Chicken Breast Butcher Counter', '1 lb', 4.99, 11.0,
                  sold_by='WEIGHT')


class PackPick(unittest.TestCase):
    def shopping(self, rows, grams):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            catalogue = {'currency': 'USD', 'venues': [], 'categories': {}, 'recipes': [
                {'id': slot, 'title': slot, 'slot': slot, 'calories': 500, 'cost': 1.0,
                 'tags': [], 'ingredients': [{'item': 'chicken breast', 'quantity': grams,
                                              'unit': 'g', 'cost': 1.0}]}
                for slot in ('breakfast', 'lunch', 'dinner')]}
            (home / 'catalogue.json').write_text(json.dumps(catalogue))
            (home / 'prices.70300123.json').write_text(json.dumps({
                'captured': '2026-09-16T04:00:00+00:00', 'location_id': '70300123',
                'items': {'chicken breast': rows}}))
            env = {**os.environ, 'HERMES_HOME': str(home),
                   'MEALS_CATALOGUE': str(home / 'catalogue.json'),
                   'MEALS_PRICES_DIR': str(home), 'MEALS_NUTRITION': str(home / 'absent.json')}
            for args in (('profile', 'set', '--people', '1', '--calories', '2000',
                          '--diet', 'none', '--store', '70300123'),
                         ('plan', '--days', '1', '--start', '2026-09-21'),
                         ('shopping', '--start', '2026-09-21')):
                run = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *args],
                                     env=env, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)['items'][0]

    def test_a_small_need_buys_the_small_pack_despite_a_worse_rate(self):
        line = self.shopping([FAMILY, TRAY], grams=380 / 3)
        self.assertEqual(line['product_id'], 'tray')
        self.assertEqual(line['packages'], 1)

    def test_a_big_need_still_buys_the_family_pack(self):
        line = self.shopping([FAMILY, TRAY], grams=3500 / 3)
        self.assertEqual(line['product_id'], 'family', 'one 17.52 pack beats six 9.99 trays')
        self.assertEqual(line['packages'], 1)

    def test_food_sold_by_weight_costs_only_the_weight_bought(self):
        line = self.shopping([FAMILY, TRAY, COUNTER], grams=380 / 3)
        self.assertEqual(line['product_id'], 'counter', '0.38 kg at 11.00 beats a 9.99 tray')

    def test_a_promotion_counts_at_the_till(self):
        on_offer = dict(FAMILY, promo=8.99)
        line = self.shopping([on_offer, TRAY], grams=380 / 3)
        self.assertEqual(line['product_id'], 'family')


if __name__ == '__main__':
    unittest.main()
