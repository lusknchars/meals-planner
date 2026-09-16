"""Contract for pricing one food, whatever it is.

Asked "how much is milk", the agent had no tool for the question: compare prices
a fixed basket and facts needs a product id. So it reached for the web and quoted
Walmart and Aldi figures it was told never to use. Kroger prices any term --
kimchi, tahini and bok choy all answered first try -- so the tool should exist.

Answers are cached: prices do not move hourly, and asking twice should not spend
a call or make somebody wait twice.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/meals/scripts'
sys.path.insert(0, str(SCRIPTS))
import price  # noqa: E402


def product(description, amount, size, product_id='p1', promo=0):
    return {'productId': product_id, 'description': description, 'brand': 'Kroger',
            'items': [{'size': size, 'soldBy': 'Unit',
                       'price': {'regular': amount, 'promo': promo}}], 'images': []}


class Recorder:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        self.requests.append(url)
        if not self.replies:
            raise AssertionError(f'no canned reply left for {url}')
        status, payload = self.replies.pop(0)
        return status, json.dumps(payload).encode()


TOKEN = {'access_token': 'tok', 'expires_in': 1800}
MILK = {'data': [product('Kroger 2% Reduced Fat Milk', 1.29, '16 fl oz', 'small'),
                 product('Kroger Whole Milk', 3.79, '1 gal', 'gallon'),
                 product('Kroger Whole Milk', 2.49, '1/2 gal', 'half')]}
EGGS = {'data': [product('Kroger Cage Free Large White Eggs', 4.39, '18 ct', 'eggs18')]}
NOTHING = {'data': []}


class Units(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def ask(self, term, replies):
        return price.price_item(Recorder(replies), 'id', 'secret', '70300022', term,
                                home=self.home)

    def test_a_liquid_is_compared_by_the_litre(self):
        found = self.ask('milk', [(200, TOKEN), (200, MILK)])
        self.assertEqual(found['unit'], 'l')
        self.assertEqual(found['best']['product_id'], 'gallon',
                         'per litre the gallon wins; the 16 fl oz only looks cheap')
        self.assertAlmostEqual(found['best']['unit_price'], 1.0, delta=0.05)

    def test_a_counted_food_is_compared_by_the_item(self):
        found = self.ask('eggs', [(200, TOKEN), (200, NOTHING), (200, NOTHING), (200, EGGS)])
        self.assertEqual(found['unit'], 'ct')
        self.assertAlmostEqual(found['best']['unit_price'], 0.24, delta=0.01)

    def test_alternatives_come_back_too_so_a_choice_is_possible(self):
        found = self.ask('milk', [(200, TOKEN), (200, MILK)])
        self.assertGreaterEqual(len(found['alternatives']), 1)
        costs = [row['unit_price'] for row in found['alternatives']]
        self.assertEqual(costs, sorted(costs), 'cheapest first')
        self.assertNotIn(found['best']['product_id'],
                         [row['product_id'] for row in found['alternatives']])

    def test_every_row_carries_what_a_reply_needs(self):
        best = self.ask('milk', [(200, TOKEN), (200, MILK)])['best']
        for field in ('product_id', 'description', 'brand', 'size', 'price',
                      'unit_price', 'unit'):
            self.assertIn(field, best, field)

    def test_nothing_relevant_is_a_plain_answer_not_a_crash(self):
        found = self.ask('unobtainium', [(200, TOKEN)] + [(200, NOTHING)] * 3)
        self.assertIsNone(found['best'])
        self.assertEqual(found['alternatives'], [])
        self.assertIn('no', (found['note'] or '').lower())

    def test_an_empty_term_is_refused(self):
        with self.assertRaises(ValueError):
            self.ask('   ', [(200, TOKEN)])


class Caching(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def ask(self, replies, ttl=None):
        recorder = Recorder(replies)
        found = price.price_item(recorder, 'id', 'secret', '70300022', 'milk',
                                 home=self.home, ttl_seconds=ttl)
        return found, recorder

    def test_asking_twice_costs_one_lookup(self):
        first, _ = self.ask([(200, TOKEN), (200, MILK)])
        second, recorder = self.ask([])
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(recorder.requests, [], 'a cached answer spends no call')
        self.assertEqual(second['best']['product_id'], first['best']['product_id'])

    def test_a_stale_answer_is_fetched_again(self):
        self.ask([(200, TOKEN), (200, MILK)])
        time.sleep(1.1)
        refreshed, recorder = self.ask([(200, TOKEN), (200, MILK)], ttl=1)
        self.assertFalse(refreshed['cached'])
        self.assertTrue(recorder.requests, 'a stale answer is looked up again')

    def test_each_store_caches_separately(self):
        price.price_item(Recorder([(200, TOKEN), (200, MILK)]), 'id', 'secret', '70300022',
                         'milk', home=self.home)
        other = price.price_item(Recorder([(200, TOKEN), (200, MILK)]), 'id', 'secret',
                                 '70400770', 'milk', home=self.home)
        self.assertFalse(other['cached'], 'another store is another price')


class WithoutAHome(unittest.TestCase):
    """HERMES_HOME names the installation whose cache these answers belong in.

    Unset, the lookup used to die with a bare ``KeyError: 'HERMES_HOME'``, which
    reaches the model as a stack-trace fragment telling it nothing it can act on.
    meals.py already refuses this by name; these do now too.
    """

    def setUp(self):
        self.saved = os.environ.pop('HERMES_HOME', None)
        self.addCleanup(lambda: os.environ.__setitem__('HERMES_HOME', self.saved)
                        if self.saved is not None else None)

    def test_pricing_without_a_home_says_which_variable_is_missing(self):
        with self.assertRaises(ValueError) as caught:
            price.price_item(Recorder([]), 'id', 'secret', '70300022', 'milk')
        self.assertIn('HERMES_HOME', str(caught.exception))

    def test_an_explicit_home_still_works_without_the_variable(self):
        with tempfile.TemporaryDirectory() as home:
            found = price.price_item(Recorder([(200, TOKEN), (200, MILK)]), 'id', 'secret',
                                     '70300022', 'milk', home=Path(home))
            self.assertIsNotNone(found['best'])


if __name__ == '__main__':
    unittest.main()
