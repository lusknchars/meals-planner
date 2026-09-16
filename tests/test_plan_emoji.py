"""Contract for the face a dish wears.

The shopping list already ships one symbol per section rather than asking the
model to choose, so a week's produce is not 🥬 one week and 🥦 the next. A plan's
meals now do the same: the emoji travels with the recipe, and the reply uses it.

A catalogue is replaceable -- MEALS_CATALOGUE points at the owner's own file --
so one without emojis has to plan, not fail.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'
SHIPPED = Path(__file__).resolve().parents[1] / 'skills/meals/catalogue.json'

BARE = {
    'currency': 'USD', 'venues': [], 'categories': {'oats': 'Pantry', 'rice': 'Pantry'},
    'recipes': [
        {'id': 'a', 'title': 'Oats', 'slot': 'breakfast', 'calories': 400, 'cost': 1.0,
         'tags': [], 'ingredients': [{'item': 'oats', 'quantity': 80, 'unit': 'g', 'cost': 1.0}]},
        {'id': 'b', 'title': 'Rice', 'slot': 'lunch', 'calories': 600, 'cost': 1.0,
         'tags': [], 'ingredients': [{'item': 'rice', 'quantity': 90, 'unit': 'g', 'cost': 1.0}]},
        {'id': 'c', 'title': 'Rice again', 'slot': 'dinner', 'calories': 700, 'cost': 1.0,
         'tags': [], 'ingredients': [{'item': 'rice', 'quantity': 90, 'unit': 'g', 'cost': 1.0}]},
    ],
}


class Shipped(unittest.TestCase):
    def setUp(self):
        self.recipes = json.loads(SHIPPED.read_text())['recipes']

    def test_every_shipped_recipe_carries_one(self):
        for recipe in self.recipes:
            with self.subTest(recipe['id']):
                self.assertTrue(recipe.get('emoji'), f"{recipe['id']} has no emoji")

    def test_a_dish_keeps_the_same_face(self):
        """Same id, same symbol -- the point of shipping it as data."""
        seen = {}
        for recipe in self.recipes:
            seen.setdefault(recipe['id'], set()).add(recipe['emoji'])
        for rid, faces in seen.items():
            self.assertEqual(len(faces), 1, f'{rid} wears {faces}')


class InThePlan(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def plan(self, catalogue):
        path = self.home / 'catalogue.json'
        path.write_text(json.dumps(catalogue))
        env = {**os.environ, 'HERMES_HOME': str(self.home), 'MEALS_CATALOGUE': str(path),
               'MEALS_NUTRITION': str(self.home / 'absent.json')}
        run = lambda *a: subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *a],
                                        env=env, capture_output=True, text=True)
        made = run('profile', 'set', '--people', '1', '--calories', '2000')
        self.assertEqual(made.returncode, 0, made.stderr)
        got = run('plan', '--days', '1', '--start', '2026-09-16')
        self.assertEqual(got.returncode, 0, got.stderr)
        return json.loads(got.stdout)['plan']

    def test_the_plan_hands_the_reply_each_meal_s_emoji(self):
        shipped = json.loads(SHIPPED.read_text())
        day = self.plan(shipped)['days'][0]
        by_id = {r['id']: r['emoji'] for r in shipped['recipes']}
        for meal in day['meals']:
            self.assertEqual(meal['emoji'], by_id[meal['recipe_id']])

    def test_a_catalogue_without_emojis_still_plans(self):
        day = self.plan(BARE)['days'][0]
        self.assertEqual([m['emoji'] for m in day['meals']], ['🍳', '🥗', '🍽️'],
                         'the slot answers when the recipe does not')


if __name__ == '__main__':
    unittest.main()
