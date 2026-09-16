"""Contract for what goes under a product photo.

A photo on its own tells you nothing you would act on. The message after it
should say what that product is for this person: the macros of a real portion,
and what share of their day it uses.

Unknowns are named. A caption that quietly omits fibre while the person's target
names 38 g of it is a half-truth, and this agent's whole discipline is that a
missing number beats a confident wrong one.
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
    'currency': 'USD', 'venues': [],
    'recipes': [{'id': 'bowl', 'title': 'Avocado bowl', 'slot': 'breakfast', 'calories': 400,
                 'cost': 2.0, 'tags': ['vegetarian'],
                 'ingredients': [{'item': 'avocado', 'quantity': 1, 'unit': 'un',
                                  'cost': 2.0}]}],
}

PRICES = {
    'captured': '2026-09-16T04:00:00+00:00', 'source': 'Kroger Products API',
    'licence': 'Kroger developer terms; not redistributed', 'location_id': '70300022',
    'items': {'avocado': [
        {'product_id': '0000000004046', 'description': 'Kroger Fresh Hass Avocados Bag',
         'brand': 'Kroger', 'size': '4 ct', 'price': 4.99, 'promo': None,
         'unit_price': 1.25, 'unit': 'ct', 'relevant': True, 'image': None}]},
}

NUTRITION = {
    'captured': '2026-09-16T04:00:00+00:00', 'source': 'USDA FoodData Central',
    'licence': 'public domain (US government work)', 'unmatched': [], 'pending': [],
    # Fibre is None because USDA publishes no fibre nutrient for this food at
    # all -- checked against fdcId 2710824, which carries only "Carbohydrates"
    # (null) and "Carbohydrate, by difference". An avocado does contain fibre;
    # this source has not measured it, and the caption says so rather than
    # printing a number nobody measured.
    'items': {'avocado': {
        'fdc_id': 2710824, 'description': 'Avocado, Hass, peeled, raw',
        'per_100g': {'kcal': 206.0, 'protein_g': 1.81, 'carb_g': 8.32, 'fat_g': 20.31,
                     'fiber_g': None, 'energy_source': 'Atwater specific factors'},
        'portions': [{'label': None, 'grams': 140.0}], 'serving_g': 140.0}},
}

WITH_FIBRE = json.loads(json.dumps(NUTRITION))
WITH_FIBRE['items']['avocado']['per_100g']['fiber_g'] = 6.7


class Facts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))
        self.prices_dir = self.home / 'snapshots'
        self.prices_dir.mkdir()
        (self.prices_dir / 'prices.70300022.json').write_text(json.dumps(PRICES))
        self.nutrition = self.home / 'nutrition.json'
        self.nutrition.write_text(json.dumps(NUTRITION))

    def call(self, *args, success=True, scope='chat-1'):
        env = {**os.environ, 'HERMES_HOME': str(self.home),
               'MEALS_CATALOGUE': str(self.catalogue),
               'MEALS_PRICES_DIR': str(self.prices_dir),
               'MEALS_NUTRITION': str(self.nutrition)}
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', scope, *args],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def profile(self, stats=True):
        args = ['profile', 'set', '--people', '1', '--diet', 'vegetarian', '--store', '70300022']
        if stats:
            args += ['--age', '30', '--sex', 'male', '--height-in', '70', '--weight-lb', '175',
                     '--activity', 'moderate', '--goal', 'maintenance']
        else:
            args += ['--calories', '2000']
        return self.call(*args)

    def test_a_portion_is_costed_and_counted(self):
        self.profile()
        found = self.call('facts', '--product-id', '0000000004046')
        self.assertEqual(found['ingredient'], 'avocado')
        self.assertEqual(found['product'], 'Kroger Fresh Hass Avocados Bag')
        self.assertEqual(found['portion_g'], 140.0)
        # 206 kcal per 100 g over a 140 g portion
        self.assertAlmostEqual(found['kcal'], 288, delta=1)
        self.assertAlmostEqual(found['fat_g'], 28.4, delta=0.2)
        self.assertIsNone(found['fiber_g'], 'USDA publishes no fibre for this food')
        self.assertIn('fiber_g', found['unknown'])
        self.assertAlmostEqual(found['price'], 4.99, places=2)
        self.assertAlmostEqual(found['unit_price'], 1.25, places=2)

    def test_the_share_of_this_persons_day_is_reported(self):
        self.profile()
        found = self.call('facts', '--product-id', '0000000004046')
        # a 2,728 kcal target, so one avocado is a bit over a tenth of it
        self.assertAlmostEqual(found['share']['calories_pct'], 11, delta=1)
        self.assertGreater(found['share']['fat_pct'], 30)
        self.assertEqual(found['target']['calories'], self.call('targets')['calories'])

    def test_without_stats_the_facts_still_come_back(self):
        self.profile(stats=False)
        found = self.call('facts', '--product-id', '0000000004046')
        self.assertAlmostEqual(found['kcal'], 288, delta=1)
        self.assertIsNone(found['share'], 'no stats means no share, not a guessed one')

    def test_a_published_nutrient_is_scaled_to_the_portion(self):
        # The same food where USDA does publish fibre: 6.7 g per 100 g over a
        # 140 g portion is 9.4 g, and nothing lands in unknown.
        self.nutrition.write_text(json.dumps(WITH_FIBRE))
        self.profile()
        found = self.call('facts', '--product-id', '0000000004046')
        self.assertAlmostEqual(found['fiber_g'], 9.4, delta=0.2)
        self.assertEqual(found['unknown'], [])

    def test_a_product_that_was_never_priced_is_refused(self):
        self.profile()
        error = self.call('facts', '--product-id', '9999999999999', success=False)
        self.assertIn('not', error['error'].lower())

    def test_an_ingredient_with_no_nutrition_says_so(self):
        self.nutrition.write_text(json.dumps({**NUTRITION, 'items': {'avocado': None}}))
        self.profile()
        found = self.call('facts', '--product-id', '0000000004046')
        self.assertIsNone(found['kcal'])
        self.assertEqual(found['unknown'], ['kcal', 'protein_g', 'carb_g', 'fat_g', 'fiber_g'])


if __name__ == '__main__':
    unittest.main()
