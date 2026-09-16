"""Contract for asking what somebody cannot eat, and which brands they buy.

The skill always said to ask about restrictions, and the agent planned without
asking anyway: a profile with no diet planned as though the person ate
everything. So the question is a gate. No plan and no order draft until the
restrictions were answered, and "none" is an answer.

Three more things are tested because each fails quietly:

* A restriction the catalogue cannot plan around (a nut allergy, say) is
  refused, never saved and ignored.
* Changing the restriction after a week was planned rebuilds the week instead
  of replaying one the person can no longer eat.
* A preferred brand is used when the store stocks it, and said when it does not,
  rather than swapped for the cheapest without a word.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/meals/scripts/meals.py'
SHIPPED = ROOT / 'skills/meals/catalogue.json'

DAIRY = {'yoghurt', 'milk', 'cottage cheese', 'cheese', 'feta', 'mozzarella', 'butter'}
ANIMAL = DAIRY | {'eggs', 'bacon', 'chicken breast', 'ground beef', 'turkey breast',
                  'salmon fillet', 'shrimp', 'tuna'}


def recipe(id, slot, tags, *items):
    return {'id': id, 'title': id, 'slot': slot, 'calories': 500, 'cost': 2.0, 'tags': tags,
            'ingredients': [{'item': item, 'quantity': 100, 'unit': 'g', 'cost': 1.0}
                            for item in items]}


CATALOGUE = {
    'currency': 'USD', 'categories': {},
    'recipes': [
        recipe('yoghurt-bowl', 'breakfast', ['vegetarian', 'gluten-free'], 'yoghurt'),
        recipe('oat-bowl', 'breakfast', ['vegetarian', 'vegan', 'gluten-free', 'dairy-free'],
               'oats'),
        recipe('chicken-rice', 'lunch', ['meat', 'gluten-free', 'dairy-free'],
               'chicken breast', 'rice'),
        recipe('lentil-rice', 'lunch', ['vegetarian', 'vegan', 'gluten-free', 'dairy-free'],
               'lentils', 'rice'),
        recipe('cheese-pasta', 'dinner', ['vegetarian'], 'pasta', 'cheese'),
        recipe('bean-stew', 'dinner', ['vegetarian', 'vegan', 'gluten-free', 'dairy-free'],
               'black beans'),
    ],
    'venues': [{'id': 'v', 'name': 'Green Corner', 'lat': 0.0, 'lon': 0.01, 'link': 'https://example.test',
                'items': [{'title': 'Falafel bowl', 'calories': 600, 'price': 9.0,
                           'tags': ['vegetarian', 'vegan']}]}],
}


def row(product_id, brand, description, unit_price):
    return {'product_id': product_id, 'brand': brand, 'description': description,
            'size': '32 oz', 'price': unit_price, 'promo': None, 'unit_price': unit_price,
            'unit': 'kg', 'relevant': True, 'image': None}


PRICES = {'captured': '2026-09-16T04:00:00+00:00', 'location_id': '70300022',
          'items': {'yoghurt': [row('1', 'Kroger', 'Kroger® Plain Yogurt', 3.3),
                                row('2', 'Chobani', 'Chobani® Plain Greek Yogurt', 11.91)],
                    'oats': [row('3', 'Kroger', 'Kroger® Old Fashioned Oats', 2.2)]}}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))
        (self.home / 'prices.70300022.json').write_text(json.dumps(PRICES))

    def call(self, *args, success=True):
        env = {**os.environ, 'HERMES_HOME': str(self.home),
               'MEALS_CATALOGUE': str(self.catalogue), 'MEALS_PRICES_DIR': str(self.home),
               'MEALS_NUTRITION': str(self.home / 'absent.json')}
        run = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *args],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0 if success else 1, run.stderr or run.stdout)
        return json.loads(run.stdout if success else run.stderr)

    def profile(self, *extra):
        return self.call('profile', 'set', '--people', '1', '--calories', '2000',
                         '--lat', '0', '--lon', '0', '--store', '70300022', *extra)['profile']

    def week(self, days='1'):
        return self.call('plan', '--days', days, '--start', '2026-09-21')


class AskingFirst(Base):
    def test_no_plan_until_restrictions_were_asked(self):
        self.profile()
        refused = self.call('plan', '--days', '1', '--start', '2026-09-21', success=False)
        for word in ('gluten', 'lactose', 'vegan', 'brand'):
            self.assertIn(word, refused['error'])

    def test_no_order_draft_until_restrictions_were_asked(self):
        self.profile()
        refused = self.call('order', '--slot', 'dinner', success=False)
        self.assertIn('restrictions', refused['error'])

    def test_none_is_an_answer(self):
        saved = self.profile('--diet', 'none')
        self.assertEqual(saved['diet'], [])
        self.assertTrue(saved['diet_asked'])
        self.assertEqual(len(self.week()['plan']['days']), 1)


class Restrictions(Base):
    def test_everyday_words_become_the_catalogue_s_tags(self):
        saved = self.profile('--diet', 'Lactose, gluten')
        self.assertEqual(saved['diet'], ['dairy-free', 'gluten-free'])

    def test_a_restriction_the_catalogue_cannot_plan_around_is_refused(self):
        refused = self.call('profile', 'set', '--people', '1', '--diet', 'nut-free',
                            success=False)
        self.assertIn('nut-free', refused['error'])
        self.assertIn('dairy-free', refused['error'], 'the refusal names what can be planned')
        self.call('profile', 'show', success=False)

    def test_none_beside_a_restriction_is_refused(self):
        self.call('profile', 'set', '--people', '1', '--diet', 'none,vegan', success=False)

    def test_a_vegan_week_holds_only_vegan_recipes(self):
        self.profile('--diet', 'vegan')
        meals = [meal['recipe_id'] for day in self.week('3')['plan']['days']
                 for meal in day['meals']]
        self.assertEqual(set(meals), {'oat-bowl', 'lentil-rice', 'bean-stew'})

    def test_a_changed_restriction_rebuilds_the_saved_week(self):
        self.profile('--diet', 'vegetarian')
        self.week()
        self.profile('--diet', 'vegan')
        again = self.week()
        self.assertFalse(again['replayed'], 'a week they can no longer eat must not replay')
        self.assertEqual(again['plan']['diet'], ['vegan'])
        self.assertEqual({meal['recipe_id'] for meal in again['plan']['days'][0]['meals']},
                         {'oat-bowl', 'lentil-rice', 'bean-stew'})


class ShippedCatalogue(unittest.TestCase):
    def setUp(self):
        self.recipes = json.loads(SHIPPED.read_text())['recipes']

    def items(self, recipe):
        return {part['item'] for part in recipe['ingredients']}

    def test_vegan_is_tagged_exactly_where_no_animal_food_is_used(self):
        for found in self.recipes:
            self.assertEqual('vegan' in found['tags'], not self.items(found) & ANIMAL,
                             found['id'])

    def test_dairy_free_is_tagged_exactly_where_no_dairy_is_used(self):
        for found in self.recipes:
            self.assertEqual('dairy-free' in found['tags'], not self.items(found) & DAIRY,
                             found['id'])

    def test_every_supported_restriction_can_fill_every_meal(self):
        for diet in (['vegetarian'], ['vegan'], ['gluten-free'], ['dairy-free'],
                     ['vegan', 'gluten-free'], ['dairy-free', 'gluten-free']):
            for slot in ('breakfast', 'lunch', 'dinner'):
                self.assertTrue(any(found['slot'] == slot and set(diet) <= set(found['tags'])
                                    for found in self.recipes), f'{diet} {slot}')


class Brands(Base):
    def shopping(self):
        self.profile('--diet', 'none')
        self.week('2')  # breakfast rotates oats, then yoghurt
        return {item['item']: item for item in self.call('shopping', '--start',
                                                          '2026-09-21')['items']}

    def test_a_brand_is_saved_and_listed(self):
        self.profile('--diet', 'none')
        self.call('brand', 'set', '--item', 'yoghurt', '--brand', 'Chobani')
        self.assertEqual(self.call('brand', 'list')['brands'],
                         [{'item': 'yoghurt', 'brand': 'Chobani'}])

    def test_an_ingredient_the_catalogue_does_not_use_is_refused_with_its_name(self):
        self.profile('--diet', 'none')
        refused = self.call('brand', 'set', '--item', 'yogurt', '--brand', 'Chobani',
                            success=False)
        self.assertIn('yoghurt', refused['error'], 'the refusal offers the catalogue name')

    def test_the_preferred_brand_is_priced_even_when_dearer(self):
        self.profile('--diet', 'none')
        self.call('brand', 'set', '--item', 'yoghurt', '--brand', 'chobani')
        yoghurt = self.shopping()['yoghurt']
        self.assertEqual(yoghurt['brand'], 'Chobani')
        self.assertEqual(yoghurt['preferred_brand'], 'chobani')
        self.assertFalse(yoghurt['brand_not_stocked'])

    def test_a_brand_the_store_does_not_stock_is_said_not_swapped_silently(self):
        self.profile('--diet', 'none')
        self.call('brand', 'set', '--item', 'yoghurt', '--brand', 'Fage')
        yoghurt = self.shopping()['yoghurt']
        self.assertEqual(yoghurt['brand'], 'Kroger', 'priced at the cheapest instead')
        self.assertTrue(yoghurt['brand_not_stocked'])

    def test_a_cleared_brand_no_longer_applies(self):
        self.profile('--diet', 'none')
        self.call('brand', 'set', '--item', 'yoghurt', '--brand', 'Chobani')
        self.call('brand', 'clear', '--item', 'yoghurt')
        self.assertEqual(self.call('brand', 'list')['brands'], [])
        self.assertIsNone(self.shopping()['yoghurt']['preferred_brand'])


if __name__ == '__main__':
    unittest.main()
