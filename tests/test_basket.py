"""Contract for a store comparison that can be trusted.

The first live run ranked three stores and two tied at exactly $15.87, which is
what a broken comparison looks like: it took the cheapest sticker price with no
check on what the product was or how much of it you got. Milk matched a 16 fl oz
single-serve at $1.29, and chicken breast matched Oscar Mayer deli slices.

So a basket staple names a standard quantity -- a gallon of milk, a dozen eggs, a
pound of rice -- and each store is costed for that same quantity. A product that
is not the staple cannot price it, and a size that will not parse cannot either.
"""
import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/meals/scripts'
sys.path.insert(0, str(SCRIPTS))
import compare  # noqa: E402
import foods  # noqa: E402


def product(description, price, size, product_id='1'):
    return {'productId': product_id, 'description': description, 'brand': 'Kroger',
            'items': [{'size': size, 'soldBy': 'Unit',
                       'price': {'regular': price, 'promo': 0}}], 'images': []}


class Staples(unittest.TestCase):
    def test_every_staple_names_a_quantity_to_compare(self):
        for staple in compare.BASKET:
            self.assertIn('term', staple)
            self.assertGreater(staple['quantity'], 0, staple['term'])
            self.assertIn(staple['unit'], ('l', 'kg', 'ct'), staple['term'])

    def test_the_basket_stays_small_enough_for_a_reply(self):
        self.assertLessEqual(len(compare.BASKET), 8)
        self.assertIn('milk', [staple['term'] for staple in compare.BASKET])


class Normalising(unittest.TestCase):
    def test_a_single_serve_cannot_undercut_a_gallon(self):
        # The live failure: $1.29 for 16 fl oz reads cheap and is not.
        rows = [product('Kroger 2% Reduced Fat Milk', 1.29, '16 fl oz', 'a'),
                product('Kroger 2% Reduced Fat Milk', 3.79, '1 gal', 'b')]
        chosen = foods.best_for({'term': 'milk', 'quantity': 1, 'unit': 'l'}, rows)
        self.assertEqual(chosen['product_id'], 'b', 'per litre, the gallon is far cheaper')
        # 3.79 for 3.785 L is about 1.00/L
        self.assertAlmostEqual(chosen['unit_price'], 1.0, delta=0.05)
        self.assertAlmostEqual(chosen['cost'], 1.0, delta=0.05)

    def test_a_staple_is_costed_for_its_standard_quantity(self):
        rows = [product('Kroger Enriched Long Grain White Rice', 1.99, '32 oz')]
        chosen = foods.best_for({'term': 'rice', 'quantity': 1, 'unit': 'kg'}, rows)
        # 1.99 for 907 g is about 2.19/kg, so a kilo costs about that
        self.assertAlmostEqual(chosen['cost'], 2.19, delta=0.05)

    def test_counted_staples_use_their_count(self):
        rows = [product('Kroger Cage Free Grade AA Large White Eggs', 4.39, '18 ct')]
        chosen = foods.best_for({'term': 'eggs', 'quantity': 12, 'unit': 'ct'}, rows)
        # 4.39 for 18 is 0.244 each, so a dozen is about 2.93
        self.assertAlmostEqual(chosen['cost'], 2.93, delta=0.05)

    def test_a_size_that_will_not_parse_cannot_price_a_staple(self):
        rows = [product('Kroger Milk', 2.49, 'family pack')]
        self.assertIsNone(foods.best_for({'term': 'milk', 'quantity': 1, 'unit': 'l'}, rows))

    def test_the_promotion_is_what_gets_compared(self):
        rows = [product('Kroger 2% Reduced Fat Milk', 3.79, '1 gal')]
        rows[0]['items'][0]['price']['promo'] = 2.79
        chosen = foods.best_for({'term': 'milk', 'quantity': 1, 'unit': 'l'}, rows)
        self.assertAlmostEqual(chosen['unit_price'], 0.74, delta=0.03)


class Relevance(unittest.TestCase):
    def test_deli_slices_cannot_stand_in_for_raw_chicken(self):
        rows = [product('Oscar Mayer Deli Fresh Rotisserie Seasoned Chicken Breast', 4.49, '9 oz'),
                product('Kroger Boneless Skinless Chicken Breast', 9.99, '2.5 lb', 'real')]
        chosen = foods.best_for({'term': 'chicken breast', 'quantity': 1, 'unit': 'kg'}, rows)
        self.assertEqual(chosen['product_id'], 'real',
                         'deli slices are cheaper per kilo and are not chicken breast')

    def test_a_product_that_does_not_name_the_staple_is_refused(self):
        rows = [product('Fresh Plantain - Single', 0.99, '1 ea')]
        self.assertIsNone(foods.best_for({'term': 'bananas', 'quantity': 1, 'unit': 'kg'}, rows))

    def test_the_staple_name_still_matches_its_plural(self):
        rows = [product('Fresh Bunch of Bananas', 0.69, '1 lb')]
        self.assertIsNotNone(foods.best_for({'term': 'bananas', 'quantity': 1, 'unit': 'kg'}, rows))


class Totals(unittest.TestCase):
    def test_a_total_sums_the_standard_quantities(self):
        priced = [{'term': 'milk', 'cost': 1.0}, {'term': 'eggs', 'cost': 2.93}]
        self.assertAlmostEqual(compare.basket_total(priced), 3.93, places=2)

    def test_a_store_that_priced_nothing_has_no_total(self):
        self.assertIsNone(compare.basket_total([]))


if __name__ == '__main__':
    unittest.main()
