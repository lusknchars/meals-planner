"""Contract for prices that belong to the person reading them.

Kroger prices US stores. The owner of this installation has a +55 number, so
every figure the agent has shown him came from a supermarket in Los Angeles he
cannot shop at. A number about the wrong country is worse than no number: it
looks exactly like a real one.

So the country comes from the person's own phone number, and store prices are
suppressed when they are not that person's stores. They are never converted:
turning a Ralphs dollar into a real gives a precise figure about a shop 10,000 km
away, which is a more confident lie than an admitted gap.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'
sys.path.insert(0, str(SCRIPT.parent))
import meals  # noqa: E402

CATALOGUE = {
    'currency': 'USD', 'venues': [],
    'categories': {'rice': 'Pantry', 'beans': 'Pantry', 'spinach': 'Produce'},
    'recipes': [
        {'id': 'oats', 'title': 'Oats', 'slot': 'breakfast', 'calories': 400, 'cost': 2.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'rice', 'quantity': 100, 'unit': 'g', 'cost': 2.0}]},
        {'id': 'bowl', 'title': 'Bean bowl', 'slot': 'lunch', 'calories': 600, 'cost': 3.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'beans', 'quantity': 100, 'unit': 'g', 'cost': 3.0}]},
        {'id': 'greens', 'title': 'Greens', 'slot': 'dinner', 'calories': 500, 'cost': 2.0,
         'tags': ['vegetarian'],
         'ingredients': [{'item': 'spinach', 'quantity': 100, 'unit': 'g', 'cost': 2.0}]},
    ],
}

PRICES = {
    'captured': '2026-09-16T04:00:00+00:00', 'source': 'Kroger Products API',
    'licence': 'Kroger developer terms; not redistributed', 'location_id': '70300022',
    'country': 'US',
    'items': {'rice': [{'product_id': 'r1', 'description': 'Kroger Long Grain Rice',
                        'brand': 'Kroger', 'size': '32 oz', 'price': 1.99, 'promo': None,
                        'unit_price': 2.19, 'unit': 'kg', 'relevant': True, 'image': None}]},
}


class PhoneNumbers(unittest.TestCase):
    def test_the_country_comes_from_the_dialling_code(self):
        self.assertEqual(meals.country_from_phone('+5511999990258'), 'BR')
        self.assertEqual(meals.country_from_phone('+16505551234'), 'US')
        self.assertEqual(meals.country_from_phone('+447700900123'), 'GB')
        self.assertEqual(meals.country_from_phone('+351912345678'), 'PT')

    def test_spacing_and_punctuation_do_not_matter(self):
        self.assertEqual(meals.country_from_phone('+55 (11) 99999-0258'), 'BR')

    def test_something_that_is_not_a_number_is_unknown_not_guessed(self):
        for bad in ('', None, 'Lucas', '5511999990258', '+999123'):
            self.assertIsNone(meals.country_from_phone(bad), bad)

    def test_a_country_names_its_currency(self):
        self.assertEqual(meals.currency_for('BR'), 'BRL')
        self.assertEqual(meals.currency_for('US'), 'USD')
        self.assertIsNone(meals.currency_for('ZZ'), 'an unknown country invents no currency')


class Suppression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps(CATALOGUE))
        self.prices_dir = self.home / 'snapshots'
        self.prices_dir.mkdir()
        (self.prices_dir / 'prices.70300022.json').write_text(json.dumps(PRICES))

    def call(self, *args, success=True, scope='chat-1'):
        env = {**os.environ, 'HERMES_HOME': str(self.home),
               'MEALS_CATALOGUE': str(self.catalogue),
               'MEALS_PRICES_DIR': str(self.prices_dir),
               'MEALS_NUTRITION': str(self.home / 'absent.json')}
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', scope, *args],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def plan_for(self, phone=None, scope='chat-1'):
        args = ['profile', 'set', '--people', '1', '--calories', '2000',
                '--diet', 'vegetarian', '--store', '70300022']
        if phone:
            args += ['--phone', phone]
        self.call(*args, scope=scope)
        self.call('plan', '--days', '1', '--start', '2026-09-16', scope=scope)
        return self.call('shopping', '--start', '2026-09-16', scope=scope)

    def test_a_brazilian_number_sees_no_united_states_shelf_prices(self):
        found = self.plan_for('+5511999990258')
        self.assertEqual(found['priced_from_snapshot'], 0)
        self.assertTrue(found['store_prices_suppressed'])
        self.assertIn('BR', found['suppressed_because'])
        self.assertIn('US', found['suppressed_because'])
        for item in found['items']:
            self.assertEqual(item['price_source'], 'catalogue estimate')

    def test_the_profile_records_what_the_number_implied(self):
        self.call('profile', 'set', '--people', '1', '--calories', '2000',
                  '--phone', '+5511999990258')
        saved = self.call('profile', 'show')['profile']
        self.assertEqual(saved['country'], 'BR')
        self.assertEqual(saved['currency'], 'BRL')

    def test_a_united_states_number_prices_normally(self):
        found = self.plan_for('+16505551234', scope='chat-us')
        self.assertEqual(found['priced_from_snapshot'], 1)
        self.assertFalse(found['store_prices_suppressed'])
        rice = {item['item']: item for item in found['items']}['rice']
        self.assertTrue(rice['price_source'].startswith('kroger:'))

    def test_no_number_at_all_still_prices(self):
        # Installations that never set a phone keep working exactly as before.
        found = self.plan_for(scope='chat-none')
        self.assertEqual(found['priced_from_snapshot'], 1)
        self.assertFalse(found['store_prices_suppressed'])

    def test_asking_for_the_other_country_prices_gets_them(self):
        # Suppression is a default, not a verdict: somebody abroad may still want
        # US prices, and saying so is recorded rather than assumed.
        self.call('profile', 'set', '--people', '1', '--calories', '2000',
                  '--diet', 'vegetarian', '--store', '70300022',
                  '--phone', '+5511999990258', '--price-country', 'US', scope='chat-asked')
        self.call('plan', '--days', '1', '--start', '2026-09-16', scope='chat-asked')
        found = self.call('shopping', '--start', '2026-09-16', scope='chat-asked')
        self.assertFalse(found['store_prices_suppressed'])
        self.assertEqual(found['priced_from_snapshot'], 1)
        saved = self.call('profile', 'show', scope='chat-asked')['profile']
        self.assertEqual(saved['country'], 'BR', 'their own country is still recorded')
        self.assertEqual(saved['price_country'], 'US', 'and so is what they asked for')

    def test_estimates_are_never_relabelled_as_local_money(self):
        # The catalogue's figures are USD-shaped. Calling them reais because the
        # reader is Brazilian would be a different lie from the one we removed.
        found = self.plan_for('+5511999990258')
        self.assertEqual(found['currency'], 'USD')
        self.assertEqual(found['estimates_currency'], 'USD')


if __name__ == '__main__':
    unittest.main()
