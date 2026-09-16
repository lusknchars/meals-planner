"""Contract for macros on a plan.

Calories were never the whole story: a target of 2,728 kcal comes with 140 g of
protein, and a plan that cannot say whether it meets that is not a plan against
the target. Macros come from the USDA snapshot, summed per day.

Two rules the tests pin down, because both are easy to get quietly wrong:

* Cost scales with household size; macros do not. Ingredient quantities are
  multiplied by ``people`` for the shopping list, but calories and macros are
  what one person eats.
* A counted ingredient contributes macros only when USDA publishes a portion
  weight. Without one, it is named as unknown rather than guessed at.
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
    'venues': [],
    'recipes': [
        {'id': 'oats-banana', 'title': 'Oats and banana', 'slot': 'breakfast',
         'calories': 500, 'cost': 3.0, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'oats', 'quantity': 100, 'unit': 'g', 'cost': 2.0},
                         {'item': 'banana', 'quantity': 1, 'unit': 'un', 'cost': 1.0}]},
        {'id': 'rice-bowl', 'title': 'Rice bowl', 'slot': 'lunch', 'calories': 600,
         'cost': 2.0, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'rice', 'quantity': 150, 'unit': 'g', 'cost': 2.0}]},
        {'id': 'tofu-plate', 'title': 'Tofu plate', 'slot': 'dinner', 'calories': 700,
         'cost': 4.0, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'tofu', 'quantity': 200, 'unit': 'g', 'cost': 4.0}]},
    ],
}

# tofu is deliberately absent: an ingredient the snapshot never matched.
NUTRITION = {
    'captured': '2026-09-16T04:00:00+00:00',
    'source': 'USDA FoodData Central',
    'licence': 'public domain (US government work)',
    'unmatched': ['tofu'],
    'pending': [],
    'items': {
        'oats': {'fdc_id': 169705, 'description': 'Oats',
                 'per_100g': {'kcal': 379.0, 'protein_g': 13.15, 'carb_g': 67.7,
                              'fat_g': 6.52, 'fiber_g': 10.1},
                 'portions': [], 'serving_g': None},
        'banana': {'fdc_id': 173944, 'description': 'Bananas, raw',
                   'per_100g': {'kcal': 89.0, 'protein_g': 1.09, 'carb_g': 22.84,
                                'fat_g': 0.33, 'fiber_g': 2.6},
                   'portions': [{'label': 'NLEA serving', 'grams': 126.0}],
                   'serving_g': 126.0},
        'rice': {'fdc_id': 169756, 'description': 'Rice, white, long-grain, raw',
                 'per_100g': {'kcal': 365.0, 'protein_g': 7.13, 'carb_g': 79.95,
                              'fat_g': 0.66, 'fiber_g': 1.3},
                 'portions': [], 'serving_g': None},
        'tofu': None,
    },
}


class DayMacros(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))
        self.nutrition = self.home / 'nutrition.json'
        self.nutrition.write_text(json.dumps(NUTRITION))

    def call(self, *args, nutrition=True, success=True, scope='chat-1'):
        env = {**os.environ, 'HERMES_HOME': str(self.home),
               'MEALS_CATALOGUE': str(self.catalogue)}
        # Point at a path that cannot exist rather than unsetting the variable:
        # the fallback is the catalogue's own directory, which is where this
        # fixture writes its snapshot, so "unset" would still find it. This is
        # the same trap MEALS_STORES sprang earlier.
        env['MEALS_NUTRITION'] = str(self.nutrition if nutrition
                                     else self.home / 'absent' / 'nutrition.json')
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', scope, *args],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def profile(self, people='1', scope='chat-1'):
        return self.call('profile', 'set', '--people', people, '--calories', '2100',
                         '--diet', 'vegetarian', scope=scope)

    def day(self, people='1', scope='chat-1', nutrition=True):
        # The scope has to reach BOTH calls: writing the profile under one scope
        # and asking for the plan under another is how this helper first failed.
        self.profile(people=people, scope=scope)
        plan = self.call('plan', '--days', '1', '--start', '2026-09-16',
                         scope=scope, nutrition=nutrition)['plan']
        return plan['days'][0]

    def test_a_day_sums_macros_from_the_snapshot(self):
        day = self.day()
        # oats 100 g + banana 126 g (1 NLEA serving) + rice 150 g
        self.assertAlmostEqual(day['protein_g'], 25.2, delta=0.2)
        self.assertAlmostEqual(day['carb_g'], 216.4, delta=0.3)
        self.assertAlmostEqual(day['fat_g'], 7.9, delta=0.2)
        self.assertAlmostEqual(day['fiber_g'], 15.3, delta=0.2)

    def test_an_ingredient_the_snapshot_never_matched_is_named(self):
        day = self.day()
        self.assertEqual(day['macros_unknown'], ['tofu'])
        self.assertFalse(day['macros_complete'],
                         'a day missing an ingredient cannot claim a complete macro count')

    def test_a_counted_ingredient_needs_a_published_portion_weight(self):
        without = json.loads(json.dumps(NUTRITION))
        without['items']['banana']['serving_g'] = None
        without['items']['banana']['portions'] = []
        self.nutrition.write_text(json.dumps(without))
        day = self.day()
        self.assertIn('banana', day['macros_unknown'])
        # Only oats and rice remain: 13.15 + 10.695
        self.assertAlmostEqual(day['protein_g'], 23.8, delta=0.2)

    def test_macros_are_per_person_while_cost_is_per_household(self):
        one = self.day(people='1', scope='single')
        two = self.day(people='2', scope='double')
        self.assertAlmostEqual(one['protein_g'], two['protein_g'], delta=0.01)
        self.assertAlmostEqual(one['calories'], two['calories'], delta=0.01)
        self.assertAlmostEqual(two['cost'], one['cost'] * 2, delta=0.01)

    def test_without_a_snapshot_a_plan_still_works(self):
        day = self.day(nutrition=False)
        self.assertEqual(day['calories'], 1800)
        self.assertIsNone(day['protein_g'])
        self.assertFalse(day['macros_complete'])
        self.assertEqual(day['macros_unknown'], ['banana', 'oats', 'rice', 'tofu'])


if __name__ == '__main__':
    unittest.main()
