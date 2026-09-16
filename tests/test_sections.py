"""Contract for a shopping list grouped the way a shop is walked.

A flat alphabetical list sends somebody from avocado to bread to beans and back
across the shop. Grouping by section is the ordinary way a list is written, and
it was in the brief from the start: produce, then bakery, meat and fish, dairy,
pantry, frozen.

An ingredient nobody has categorised still has to appear. Dropping a line
because its section is unknown would lose food from a shopping trip, which is
worse than putting it under "Other".
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
    'categories': {'avocado': 'Produce', 'spinach': 'Produce', 'bread': 'Bakery',
                   'chicken breast': 'Meat & Fish', 'yoghurt': 'Dairy', 'rice': 'Pantry'},
    'recipes': [
        {'id': 'toast', 'title': 'Avocado toast', 'slot': 'breakfast', 'calories': 400,
         'cost': 3.0, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'avocado', 'quantity': 1, 'unit': 'un', 'cost': 1.5},
                         {'item': 'bread', 'quantity': 2, 'unit': 'sl', 'cost': 1.0},
                         {'item': 'yoghurt', 'quantity': 100, 'unit': 'g', 'cost': 0.5}]},
        {'id': 'bowl', 'title': 'Rice bowl', 'slot': 'lunch', 'calories': 600, 'cost': 2.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'rice', 'quantity': 100, 'unit': 'g', 'cost': 1.0},
                         {'item': 'spinach', 'quantity': 80, 'unit': 'g', 'cost': 1.0}]},
        {'id': 'supper', 'title': 'Greens and seeds', 'slot': 'dinner', 'calories': 700,
         'cost': 4.0, 'tags': ['vegetarian'],
         'ingredients': [{'item': 'spinach', 'quantity': 100, 'unit': 'g', 'cost': 1.0},
                         # deliberately uncategorised
                         {'item': 'dukkah', 'quantity': 20, 'unit': 'g', 'cost': 3.0}]},
    ],
}


class Sections(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))

    def call(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *args],
                                env={**os.environ, 'HERMES_HOME': str(self.home),
                                     'MEALS_CATALOGUE': str(self.catalogue),
                                     'MEALS_NUTRITION': str(self.home / 'absent.json'),
                                     'MEALS_PRICES_DIR': str(self.home / 'absent')},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def shopping(self):
        self.call('profile', 'set', '--people', '1', '--calories', '2000',
                  '--diet', 'vegetarian')
        self.call('plan', '--days', '1', '--start', '2026-09-16')
        return self.call('shopping', '--start', '2026-09-16')

    def test_the_list_is_grouped_into_sections(self):
        found = self.shopping()
        names = [section['name'] for section in found['sections']]
        self.assertIn('Produce', names)
        self.assertIn('Bakery', names)
        self.assertIn('Dairy', names)

    def test_sections_come_in_the_order_a_shop_is_walked(self):
        found = self.shopping()
        names = [section['name'] for section in found['sections']]
        self.assertEqual(names, sorted(names, key=lambda name: SECTION_ORDER.index(name)),
                         'produce first, other last')
        self.assertEqual(names[0], 'Produce')
        self.assertEqual(names[-1], 'Other')

    def test_every_section_carries_its_own_symbol(self):
        found = self.shopping()
        symbols = {section['name']: section['emoji'] for section in found['sections']}
        self.assertEqual(symbols['Produce'], '🥬')
        self.assertEqual(symbols['Dairy'], '🥛')
        self.assertEqual(symbols['Other'], '✨')
        # Fixed here, not chosen by the model: the same food should not be a
        # different symbol each week.
        self.assertEqual(len(set(symbols.values())), len(symbols))

    def test_an_uncategorised_ingredient_is_still_on_the_list(self):
        found = self.shopping()
        other = [s for s in found['sections'] if s['name'] == 'Other'][0]
        self.assertEqual([item['item'] for item in other['items']], ['dukkah'])

    def test_every_item_appears_exactly_once(self):
        found = self.shopping()
        grouped = [item['item'] for section in found['sections'] for item in section['items']]
        self.assertEqual(sorted(grouped), sorted(item['item'] for item in found['items']))
        self.assertEqual(len(grouped), len(set(grouped)))

    def test_a_section_carries_its_own_total(self):
        found = self.shopping()
        for section in found['sections']:
            self.assertAlmostEqual(section['cost'],
                                   round(sum(item['cost'] for item in section['items']), 2),
                                   places=2, msg=section['name'])
        self.assertAlmostEqual(sum(s['cost'] for s in found['sections']),
                               found['total_cost'], places=2)

    def test_the_flat_list_is_still_there_for_anything_that_wants_it(self):
        found = self.shopping()
        self.assertEqual([item['item'] for item in found['items']],
                         sorted(item['item'] for item in found['items']))


SECTION_ORDER = ('Produce', 'Bakery', 'Meat & Fish', 'Dairy', 'Pantry', 'Frozen', 'Other')


if __name__ == '__main__':
    unittest.main()
