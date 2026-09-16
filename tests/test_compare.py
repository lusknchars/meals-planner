"""Contract for comparing nearby stores on a small basket.

"Best prices in my region" has an expensive reading and a cheap one. Pricing
every catalogue ingredient at every nearby store is 76 calls and two minutes per
store -- unusable in a reply. Pricing eight staples is eight calls per store, so
three stores answer in seconds, and that is what tells somebody where to shop.

No network here: fetch is injected, and the payloads mirror real Kroger
responses seen on 2026-09-16.
"""
import json
from pathlib import Path
import sys
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/compare.py'
sys.path.insert(0, str(SCRIPT.parent))
import compare  # noqa: E402

TOKEN_REPLY = {'access_token': 'tok_abc', 'expires_in': 1800}

LOCATIONS_REPLY = {'data': [
    {'locationId': '70300022', 'chain': 'RALPHS', 'name': 'Ralphs Fresh Fare - 9th Flower',
     'address': {'addressLine1': '645 W 9Th St', 'city': 'Los Angeles', 'zipCode': '90015'},
     'geolocation': {'latitude': 34.0444, 'longitude': -118.2611},
     'phone': '2134520840',
     'hours': {'timezone': 'America/Los_Angeles',
               'monday': {'open': '05:00', 'close': '00:00', 'open24': False}}},
    {'locationId': '70400770', 'chain': 'FOOD4LESS', 'name': 'Food 4 Less - 6th Burlington',
     'address': {'addressLine1': '1700 W 6Th St', 'city': 'Los Angeles', 'zipCode': '90017'},
     'geolocation': {'latitude': 34.0600, 'longitude': -118.2750},
     'phone': '2133887005',
     'hours': {'timezone': 'America/Los_Angeles',
               'monday': {'open': '06:00', 'close': '23:00', 'open24': False}}},
]}


def products(price, description=None, size=None, staple=None):
    """A search result that satisfies whichever staple is being priced.

    The old fixture answered every staple with a gallon of milk, which the
    normalising comparison correctly refuses: a gallon cannot price a dozen eggs.
    """
    staple = staple or {'term': 'milk', 'quantity': 1, 'unit': 'l'}
    if size is None:
        size = {'l': '1 gal', 'kg': '2 lb', 'ct': '12 ct'}[staple['unit']]
    return {'data': [{'productId': '0001111041600',
                      'description': description or f"Kroger {staple['term']}",
                      'brand': 'Kroger',
                      'items': [{'size': size, 'soldBy': 'Unit',
                                 'price': {'regular': price, 'promo': 0}}],
                      'images': []}]}


def basket_replies(price):
    """One search reply per staple, each shaped for that staple's unit."""
    return [(200, products(price, staple=staple)) for staple in compare.BASKET]


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


class Basket(unittest.TestCase):
    def test_the_basket_is_small_enough_to_answer_in_a_reply(self):
        self.assertLessEqual(len(compare.BASKET), 8)
        self.assertGreaterEqual(len(compare.BASKET), 5)
        self.assertIn('eggs', [staple['term'] for staple in compare.BASKET])

    def test_a_store_total_sums_the_standard_quantities(self):
        # Normalised rows carry the cost of the staple's standard quantity, not
        # the sticker price of whichever packet was cheapest.
        priced = [{'term': 'milk', 'cost': 1.0}, {'term': 'eggs', 'cost': 2.93}]
        self.assertAlmostEqual(compare.basket_total(priced), 3.93, places=2)

    def test_a_store_that_priced_nothing_has_no_total(self):
        self.assertIsNone(compare.basket_total([]))


class Comparing(unittest.TestCase):
    def replies(self, first_price, second_price):
        # token, locations, then one search per staple for each of two stores.
        # Each reply is shaped for its own staple: answering every one with a
        # gallon of milk is what the normalising comparison exists to refuse.
        return ([(200, TOKEN_REPLY), (200, LOCATIONS_REPLY)]
                + basket_replies(first_price) + basket_replies(second_price))

    def test_stores_are_ranked_by_what_the_basket_costs(self):
        fetch = Recorder(self.replies(4.00, 3.00))
        result = compare.compare_stores(fetch, 'id', 'secret', '90012', limit=2)
        ranked = result['stores']
        self.assertEqual(ranked[0]['location_id'], '70400770', 'the cheaper basket ranks first')
        self.assertLess(ranked[0]['basket_total'], ranked[1]['basket_total'])
        self.assertEqual(result['basket'], list(compare.BASKET))

    def test_each_store_carries_what_helps_somebody_choose(self):
        fetch = Recorder(self.replies(4.00, 3.00))
        store = compare.compare_stores(fetch, 'id', 'secret', '90012', limit=2)['stores'][0]
        self.assertEqual(store['name'], 'Food 4 Less - 6th Burlington')
        self.assertEqual(store['address'], '1700 W 6Th St')
        self.assertEqual(store['chain'], 'FOOD4LESS')
        self.assertIn('priced', store)
        self.assertEqual(store['priced'], len(compare.BASKET))

    def test_distance_is_reported_when_a_location_is_known(self):
        fetch = Recorder(self.replies(4.00, 3.00))
        result = compare.compare_stores(fetch, 'id', 'secret', '90012', limit=2,
                                        lat=34.0522, lon=-118.2437)
        for store in result['stores']:
            self.assertGreater(store['distance_km'], 0)
            self.assertLess(store['distance_km'], 10)

    def test_the_call_count_stays_proportional_to_the_basket(self):
        fetch = Recorder(self.replies(4.00, 3.00))
        compare.compare_stores(fetch, 'id', 'secret', '90012', limit=2)
        searches = [url for url in fetch.requests if '/products' in url]
        self.assertEqual(len(searches), 2 * len(compare.BASKET),
                         'one search per staple per store, and nothing more')

    def test_a_store_with_no_prices_is_reported_not_ranked_first(self):
        calls = [(200, TOKEN_REPLY), (200, LOCATIONS_REPLY)]
        calls += [(200, {'data': []})] * len(compare.BASKET)
        calls += [(200, products(5.00))] * len(compare.BASKET)
        result = compare.compare_stores(Recorder(calls), 'id', 'secret', '90012', limit=2)
        first, second = result['stores']
        self.assertEqual(first['location_id'], '70400770')
        self.assertIsNone(second['basket_total'])
        self.assertEqual(second['priced'], 0)


if __name__ == '__main__':
    unittest.main()
