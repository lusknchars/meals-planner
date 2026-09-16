"""Contract for handing the week's shopping to Instacart.

The agent holds no card and places nothing. Instacart's shopping list page takes
the list and gives back a link; the person opens it, picks a store and pays in
Instacart. What is tested is what could go wrong on the way:

* quantities in units Instacart does not match, which fail silently there;
* a link built for somebody Instacart does not deliver to;
* a link that is not Instacart's, passed on into somebody's chat;
* a fresh page spent on every ask, which Instacart asks callers not to do.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/meals/scripts'
SCRIPT = SCRIPTS / 'instacart.py'
MEALS = SCRIPTS / 'meals.py'
sys.path.insert(0, str(SCRIPTS))
import instacart  # noqa: E402

LINK = 'https://customers.dev.instacart.tools/store/shopping_lists/1234'
CATALOGUE = {'currency': 'USD', 'venues': [], 'categories': {}, 'recipes': [
    {'id': 'b', 'title': 'Toast', 'slot': 'breakfast', 'calories': 400, 'cost': 1.0,
     'tags': [], 'ingredients': [
         {'item': 'bread', 'quantity': 2, 'unit': 'sl', 'cost': 0.4},
         {'item': 'avocado', 'quantity': 0.5, 'unit': 'un', 'cost': 0.6}]},
    {'id': 'l', 'title': 'Courgette salad', 'slot': 'lunch', 'calories': 500, 'cost': 2.0,
     'tags': [], 'ingredients': [
         {'item': 'courgette', 'quantity': 150, 'unit': 'g', 'cost': 0.8}]},
    {'id': 'd', 'title': 'Milk rice', 'slot': 'dinner', 'calories': 600, 'cost': 1.5,
     'tags': [], 'ingredients': [
         {'item': 'milk', 'quantity': 200, 'unit': 'ml', 'cost': 0.3}]}]}


def shopping_items(*items):
    return {'items': [dict(item=name, quantity=quantity, unit=unit)
                      for name, quantity, unit in items]}


class LineItems(unittest.TestCase):
    def test_weights_and_volumes_use_units_instacart_matches(self):
        lines, _ = instacart.line_items(shopping_items(('rice', 450.0, 'g'),
                                                       ('milk', 1400.0, 'ml')))
        self.assertEqual(lines[0], {'name': 'rice', 'quantity': 450, 'unit': 'gram'})
        self.assertEqual(lines[1], {'name': 'milk', 'quantity': 1400, 'unit': 'milliliter'})

    def test_counted_food_is_bought_whole(self):
        lines, _ = instacart.line_items(shopping_items(('avocado', 1.5, 'un')))
        self.assertEqual(lines[0], {'name': 'avocado', 'quantity': 2, 'unit': 'each'})

    def test_a_unit_instacart_has_no_word_for_is_named_not_converted(self):
        lines, unmeasured = instacart.line_items(shopping_items(('bread', 14.0, 'sl')))
        self.assertEqual(lines[0], {'name': 'bread', 'display_text': 'bread, 14 slices'},
                         'a slice count must not become 14 loaves')
        self.assertEqual(unmeasured, ['bread'])

    def test_a_preferred_brand_filters_that_line(self):
        items = shopping_items(('yoghurt', 600.0, 'g'), ('rice', 450.0, 'g'))
        items['items'][0]['preferred_brand'] = 'Chobani'
        lines, _ = instacart.line_items(items)
        self.assertEqual(lines[0]['filters'], {'brand_filters': ['Chobani']})
        self.assertNotIn('filters', lines[1])

    def test_british_names_are_searched_by_their_american_names(self):
        lines, _ = instacart.line_items(shopping_items(('courgette', 300.0, 'g')))
        self.assertEqual(lines[0]['name'], 'zucchini')


class Link(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        catalogue = self.home / 'catalogue.json'
        catalogue.write_text(json.dumps(CATALOGUE))
        self.env = {'HERMES_HOME': str(self.home), 'MEALS_CATALOGUE': str(catalogue),
                    'MEALS_PRICES_DIR': str(self.home)}
        patched = mock.patch.dict(os.environ, self.env)
        patched.start()
        self.addCleanup(patched.stop)
        self.calls = []

    def meals(self, *args):
        run = subprocess.run([sys.executable, str(MEALS), '--scope', 'chat-1', *args],
                             env={**os.environ, **self.env}, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def planned(self, *profile):
        self.meals('profile', 'set', '--people', '1', '--calories', '2000', '--diet', 'none',
                   *profile)
        self.meals('plan', '--days', '2', '--start', '2026-09-14')

    def fetch(self, status=200, url=LINK):
        def caller(method, address, headers=None, body=None, timeout=None):
            self.calls.append({'method': method, 'url': address, 'headers': headers,
                               'body': json.loads(body)})
            return status, json.dumps({'products_link_url': url}).encode()
        return caller

    def link(self, fetch=None, key='keys.test', environment='development'):
        return instacart.shopping_link(fetch or self.fetch(), 'chat-1', '2026-09-14',
                                       key=key, environment=environment)

    def test_the_saved_list_becomes_one_instacart_link(self):
        self.planned()
        found = self.link()
        self.assertEqual(found['url'], LINK)
        self.assertEqual(found['unmeasured'], ['bread'])
        call = self.calls[0]
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['url'],
                         'https://connect.dev.instacart.tools/idp/v1/products/products_link')
        self.assertEqual(call['headers']['Authorization'], 'Bearer keys.test')
        self.assertEqual(call['body']['link_type'], 'shopping_list')
        names = sorted(line['name'] for line in call['body']['line_items'])
        self.assertEqual(names, ['avocado', 'bread', 'milk', 'zucchini'])

    def test_a_production_key_goes_to_the_production_server(self):
        self.planned()
        self.link(environment='production')
        self.assertTrue(self.calls[0]['url'].startswith('https://connect.instacart.com/'))

    def test_without_a_key_nothing_is_sent(self):
        self.planned()
        with self.assertRaises(ValueError) as caught:
            self.link(key='')
        self.assertIn('INSTACART_API_KEY', str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_a_number_instacart_does_not_deliver_to_gets_no_link(self):
        self.planned('--phone', '+55 11 91234 5678')
        with self.assertRaises(ValueError) as caught:
            self.link()
        self.assertIn('BR', str(caught.exception))
        self.assertEqual(self.calls, [], 'nothing is built for somebody it cannot serve')

    def test_the_same_list_twice_reuses_the_link(self):
        self.planned()
        self.link()
        again = self.link()
        self.assertEqual(len(self.calls), 1, 'Instacart asks for a new page only on change')
        self.assertTrue(again['cached'])
        self.assertEqual(again['url'], LINK)

    def test_a_refused_key_says_so_without_repeating_it(self):
        self.planned()
        with self.assertRaises(ValueError) as caught:
            self.link(fetch=self.fetch(status=401), key='keys.secret')
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('keys.secret', str(caught.exception))

    def test_a_link_that_is_not_instacarts_is_not_passed_on(self):
        self.planned()
        with self.assertRaises(ValueError):
            self.link(fetch=self.fetch(url='https://instacart.com.evil.test/list'))
        with self.assertRaises(ValueError):
            self.link(fetch=self.fetch(url='http://www.instacart.com/store/list'))


class CommandLine(unittest.TestCase):
    def test_a_missing_key_exits_nonzero_with_json_on_stderr(self):
        with tempfile.TemporaryDirectory() as home:
            env = {key: value for key, value in os.environ.items()
                   if key != 'INSTACART_API_KEY'}
            run = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1'],
                                 env={**env, 'HERMES_HOME': home},
                                 capture_output=True, text=True)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn('INSTACART_API_KEY', json.loads(run.stderr)['error'])


if __name__ == '__main__':
    unittest.main()
