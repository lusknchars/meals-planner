import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'
CATALOGUE = Path(__file__).resolve().parents[1] / 'skills/meals/catalogue.json'

FIXTURE = {
    'currency': 'USD',
    'recipes': [
        {'id': 'oats', 'title': 'Oats and berries', 'slot': 'breakfast', 'calories': 420,
         'cost': 2.1, 'tags': ['vegetarian', 'gluten-free'],
         'ingredients': [{'item': 'oats', 'quantity': 80, 'unit': 'g', 'cost': 0.6},
                         {'item': 'berries', 'quantity': 100, 'unit': 'g', 'cost': 1.5}]},
        {'id': 'eggs', 'title': 'Scrambled eggs', 'slot': 'breakfast', 'calories': 450,
         'cost': 2.4, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'eggs', 'quantity': 3, 'unit': 'un', 'cost': 1.2},
                         {'item': 'butter', 'quantity': 10, 'unit': 'g', 'cost': 1.2}]},
        {'id': 'chicken-rice', 'title': 'Chicken and rice', 'slot': 'lunch', 'calories': 700,
         'cost': 4.0, 'tags': ['meat'],
         'ingredients': [{'item': 'chicken breast', 'quantity': 180, 'unit': 'g', 'cost': 2.8},
                         {'item': 'rice', 'quantity': 90, 'unit': 'g', 'cost': 1.2}]},
        {'id': 'lentil-bowl', 'title': 'Lentil bowl', 'slot': 'lunch', 'calories': 650,
         'cost': 3.0, 'tags': ['vegetarian', 'gluten-free'],
         'ingredients': [{'item': 'lentils', 'quantity': 120, 'unit': 'g', 'cost': 1.4},
                         {'item': 'rice', 'quantity': 90, 'unit': 'g', 'cost': 1.2},
                         {'item': 'spinach', 'quantity': 60, 'unit': 'g', 'cost': 0.4}]},
        {'id': 'salmon', 'title': 'Salmon and greens', 'slot': 'dinner', 'calories': 780,
         'cost': 6.5, 'tags': ['fish', 'gluten-free'],
         'ingredients': [{'item': 'salmon fillet', 'quantity': 160, 'unit': 'g', 'cost': 5.5},
                         {'item': 'spinach', 'quantity': 80, 'unit': 'g', 'cost': 1.0}]},
        {'id': 'tofu-stirfry', 'title': 'Tofu stir fry', 'slot': 'dinner', 'calories': 690,
         'cost': 3.6, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'tofu', 'quantity': 200, 'unit': 'g', 'cost': 2.4},
                         {'item': 'rice', 'quantity': 90, 'unit': 'g', 'cost': 1.2}]},
    ],
    'venues': [
        {'id': 'near-veg', 'name': 'Green Corner', 'cuisine': 'salads', 'lat': 0.0, 'lon': 0.02,
         'eta_min': 20, 'link': 'https://example.test/order/near-veg',
         'items': [{'title': 'Falafel bowl', 'calories': 640, 'price': 11.0, 'tags': ['vegetarian']}]},
        {'id': 'far-cheap', 'name': 'Distant Diner', 'cuisine': 'burgers', 'lat': 0.0, 'lon': 0.9,
         'eta_min': 55, 'link': 'https://example.test/order/far-cheap',
         'items': [{'title': 'Veggie burger', 'calories': 620, 'price': 8.0, 'tags': ['vegetarian']}]},
        {'id': 'near-meat', 'name': 'Grill House', 'cuisine': 'grill', 'lat': 0.01, 'lon': 0.0,
         'eta_min': 25, 'link': 'https://example.test/order/near-meat',
         'items': [{'title': 'Steak plate', 'calories': 1100, 'price': 16.0, 'tags': ['meat']}]},
    ],
}


class MealsFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(FIXTURE))

    def call(self, *args, scope='chat-1', success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', scope, *args],
                                env={**os.environ, 'HERMES_HOME': str(self.home),
                                     'MEALS_CATALOGUE': str(self.catalogue)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def profile(self, scope='chat-1', **overrides):
        args = {'--people': '2', '--calories': '2000', '--budget': '120', '--diet': 'vegetarian',
                '--lat': '0.0', '--lon': '0.0'}
        args.update(overrides)
        flat = [value for pair in args.items() for value in pair]
        return self.call('profile', 'set', *flat, scope=scope)['profile']

    # Profile

    def test_profile_saves_and_reads_back(self):
        saved = self.profile()
        self.assertEqual(saved['people'], 2)
        self.assertEqual(saved['calories'], 2000)
        self.assertEqual(saved['diet'], ['vegetarian'])
        self.assertEqual(self.call('profile', 'show')['profile'], saved)

    def test_plan_requires_a_profile(self):
        error = self.call('plan', '--days', '3', success=False)
        self.assertIn('profile', error['error'].lower())

    # Weekly plan

    def test_plan_honours_diet_calories_and_is_replayable(self):
        self.profile()
        plan = self.call('plan', '--days', '7', '--start', '2026-09-16')['plan']
        self.assertEqual(len(plan['days']), 7)
        titles = [meal['recipe_id'] for day in plan['days'] for meal in day['meals']]
        self.assertNotIn('chicken-rice', titles, 'meat recipe served to a vegetarian profile')
        self.assertNotIn('salmon', titles, 'fish recipe served to a vegetarian profile')
        for day in plan['days']:
            self.assertEqual([meal['slot'] for meal in day['meals']],
                             ['breakfast', 'lunch', 'dinner'])
            self.assertEqual(day['calories'], sum(meal['calories'] for meal in day['meals']))
            self.assertLessEqual(abs(day['calories'] - 2000), 300, day)
        self.assertAlmostEqual(plan['total_cost'],
                               round(sum(day['cost'] for day in plan['days']), 2), places=2)
        again = self.call('plan', '--days', '7', '--start', '2026-09-16')['plan']
        self.assertEqual(again, plan, 'the same week must not be replanned on retry')
        self.assertEqual(self.call('plan', 'show', '--start', '2026-09-16')['plan'], plan)

    def test_plan_reports_going_over_budget(self):
        self.profile(**{'--budget': '10'})
        plan = self.call('plan', '--days', '7', '--start', '2026-09-16')['plan']
        self.assertTrue(plan['over_budget'])
        self.assertGreater(plan['total_cost'], plan['budget'])

    def test_shopping_list_aggregates_ingredients(self):
        self.profile()
        plan = self.call('plan', '--days', '3', '--start', '2026-09-16')['plan']
        shopping = self.call('shopping', '--start', '2026-09-16')
        rice = [item for item in shopping['items'] if item['item'] == 'rice']
        self.assertTrue(rice, shopping)
        self.assertEqual(rice[0]['unit'], 'g')
        self.assertAlmostEqual(shopping['total_cost'], plan['total_cost'], places=2)
        self.assertEqual(shopping['items'], sorted(shopping['items'], key=lambda i: i['item']))

    # Ordering

    def test_order_prefers_close_cheap_and_fitting(self):
        self.profile()
        order = self.call('order', '--slot', 'dinner', '--date', '2026-09-16')['order']
        self.assertEqual(order['venue_id'], 'near-veg', 'closest fitting vegetarian venue')
        self.assertGreater(order['distance_km'], 0)
        self.assertEqual(order['status'], 'draft')
        self.assertTrue(order['link'].startswith('https://'))
        self.assertNotIn('near-meat', [option['venue_id'] for option in order['alternatives']])

    def test_order_respects_max_distance(self):
        self.profile()
        error = self.call('order', '--slot', 'dinner', '--date', '2026-09-16',
                          '--max-distance-km', '0.5', '--craving', 'burgers', success=False)
        self.assertIn('distance', error['error'].lower())

    def test_confirming_an_order_records_it_against_the_day(self):
        self.profile()
        order = self.call('order', '--slot', 'dinner', '--date', '2026-09-16')['order']
        placed = self.call('order', 'confirm', order['id'])['order']
        self.assertEqual(placed['status'], 'placed')
        today = self.call('today', '--date', '2026-09-16')
        self.assertEqual(today['consumed'], order['calories'])
        self.assertEqual(self.call('order', 'list')['orders'][0]['id'], order['id'])

    # Tracking

    def test_logging_meals_counts_against_the_target(self):
        self.profile()
        self.call('log', '--title', 'Pastel at the market', '--calories', '520',
                  '--date', '2026-09-16')
        today = self.call('today', '--date', '2026-09-16')
        self.assertEqual(today['target'], 2000)
        self.assertEqual(today['consumed'], 520)
        self.assertEqual(today['remaining'], 1480)
        self.assertFalse(today['over_target'])
        self.call('log', '--title', 'Feijoada', '--calories', '1600', '--date', '2026-09-16')
        over = self.call('today', '--date', '2026-09-16')
        self.assertTrue(over['over_target'])
        self.assertEqual(over['remaining'], 0)

    # Isolation and storage

    def test_each_conversation_keeps_its_own_records(self):
        self.profile()
        self.call('plan', '--days', '3', '--start', '2026-09-16')
        self.call('profile', 'show', scope='chat-2', success=False)
        self.assertEqual(self.call('order', 'list', scope='chat-2')['orders'], [])

    def test_store_is_private_to_the_installation(self):
        self.profile()
        mode = (self.home / 'meals/meals.sqlite3').stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class ShippedCatalogue(unittest.TestCase):
    def test_catalogue_is_complete(self):
        data = json.loads(CATALOGUE.read_text())
        slots = {'breakfast', 'lunch', 'dinner'}
        self.assertTrue(data['recipes'], 'no recipes shipped')
        for recipe in data['recipes']:
            self.assertIn(recipe['slot'], slots, recipe)
            self.assertGreater(recipe['calories'], 0, recipe)
            self.assertGreater(recipe['cost'], 0, recipe)
            self.assertTrue(recipe['ingredients'], recipe)
            self.assertAlmostEqual(recipe['cost'],
                                   round(sum(i['cost'] for i in recipe['ingredients']), 2),
                                   places=2, msg=recipe['id'])
        for slot in slots:
            vegetarian = [r for r in data['recipes']
                          if r['slot'] == slot and 'vegetarian' in r['tags']]
            self.assertGreaterEqual(len(vegetarian), 3, f'too few vegetarian {slot} options')
        for venue in data['venues']:
            self.assertTrue(venue['link'].startswith('https://'), venue)
            self.assertTrue(venue['items'], venue)
            for item in venue['items']:
                self.assertGreater(item['calories'], 0, item)
                self.assertGreater(item['price'], 0, item)


if __name__ == '__main__':
    unittest.main()
