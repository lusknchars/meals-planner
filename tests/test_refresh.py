"""Contract for refresh.py: the out-of-band snapshot builder.

Nothing here touches the network. Every function that would fetch takes a
``fetch`` callable, so these tests record what WOULD be sent and answer with
payloads shaped like the real ones (verified against live responses on
2026-09-16: Overpass elements, Kroger token/locations/products).
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import refresh  # noqa: E402


class Recorder:
    """Stands in for refresh's HTTP call: records requests, returns canned replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        self.requests.append({'method': method, 'url': url,
                              'headers': dict(headers or {}), 'body': body})
        if not self.replies:
            raise AssertionError(f'no canned reply left for {method} {url}')
        status, payload = self.replies.pop(0)
        return status, json.dumps(payload).encode() if not isinstance(payload, bytes) else payload


OVERPASS_REPLY = {'elements': [
    {'type': 'node', 'id': 1, 'lat': 37.7768, 'lon': -122.3941,
     'tags': {'name': 'Safeway', 'brand': 'Safeway', 'shop': 'supermarket',
              'addr:housenumber': '298', 'addr:street': 'King Street',
              'opening_hours': 'Su-Sa 06:00-22:00'}},
    {'type': 'way', 'id': 2, 'center': {'lat': 37.7811, 'lon': -122.3998},
     'tags': {'name': 'Whole Foods Market', 'shop': 'supermarket',
              'addr:housenumber': '399', 'addr:street': '4th Street'}},
    {'type': 'node', 'id': 3, 'lat': 37.78, 'lon': -122.41,
     'tags': {'shop': 'supermarket'}},  # unnamed: not a place anyone can be sent to
]}

TOKEN_REPLY = {'access_token': 'tok_abc', 'expires_in': 1800, 'token_type': 'bearer'}

LOCATIONS_REPLY = {'data': [
    {'locationId': '70100123', 'chain': 'RALPHS', 'name': 'Ralphs Wilshire',
     'address': {'addressLine1': '1233 Wilshire Blvd', 'city': 'Los Angeles',
                 'state': 'CA', 'zipCode': '90017'},
     'geolocation': {'latitude': 34.0522, 'longitude': -118.2637}},
]}

PRODUCTS_REPLY = {'data': [
    {'productId': '0001111041600', 'description': 'Kroger 2% Reduced Fat Milk', 'brand': 'Kroger',
     'items': [{'size': '1/2 gal', 'soldBy': 'Unit', 'price': {'regular': 2.29, 'promo': 0}}],
     'images': [{'perspective': 'front', 'sizes': [{'size': 'medium',
                 'url': 'https://www.kroger.com/product/images/medium/front/0001111041600'}]}]},
    {'productId': '0002222033300', 'description': 'Simple Truth Organic Rice', 'brand': 'Simple Truth',
     'items': [{'size': '32 oz', 'soldBy': 'Unit', 'price': {'regular': 4.49, 'promo': 3.99}}],
     'images': []},
]}


class Stores(unittest.TestCase):
    def test_query_asks_for_one_radius_around_a_point(self):
        query = refresh.overpass_query(34.05, -118.25, radius_km=5)
        self.assertIn('around:5000,34.05,-118.25', query)
        self.assertIn('supermarket', query)
        self.assertIn('out center', query, 'ways need a centre or they have no coordinates')

    def test_elements_become_addressable_stores(self):
        stores = refresh.store_records(OVERPASS_REPLY['elements'])
        self.assertEqual([store['id'] for store in stores], ['osm:node/1', 'osm:way/2'])
        first = stores[0]
        self.assertEqual(first['name'], 'Safeway')
        self.assertEqual(first['brand'], 'Safeway')
        self.assertEqual(first['address'], '298 King Street')
        self.assertEqual(first['hours'], 'Su-Sa 06:00-22:00')
        self.assertAlmostEqual(first['lat'], 37.7768)
        self.assertEqual(refresh.store_records(OVERPASS_REPLY['elements'])[1]['lat'], 37.7811,
                         'a way carries its coordinates in center')

    def test_stores_sort_by_distance_and_cut_to_a_limit(self):
        stores = refresh.store_records(OVERPASS_REPLY['elements'])
        near = refresh.nearest(stores, 37.7811, -122.3998, limit=1)
        self.assertEqual(len(near), 1)
        self.assertEqual(near[0]['name'], 'Whole Foods Market')
        self.assertEqual(near[0]['distance_km'], 0.0)

    def test_fetching_stores_records_one_overpass_call(self):
        fetch = Recorder([(200, OVERPASS_REPLY)])
        stores = refresh.fetch_stores(fetch, 34.05, -118.25, radius_km=5)
        self.assertEqual(len(stores), 2)
        self.assertEqual(fetch.requests[0]['method'], 'POST')
        self.assertIn('overpass', fetch.requests[0]['url'])
        self.assertIn('User-Agent', fetch.requests[0]['headers'])


class KrogerAuth(unittest.TestCase):
    def test_token_uses_basic_auth_and_the_product_scope(self):
        fetch = Recorder([(200, TOKEN_REPLY)])
        token = refresh.kroger_token(fetch, 'id-1', 'secret-1')
        self.assertEqual(token, 'tok_abc')
        sent = fetch.requests[0]
        self.assertEqual(sent['url'], 'https://api.kroger.com/v1/connect/oauth2/token')
        self.assertTrue(sent['headers']['Authorization'].startswith('Basic '))
        self.assertIn(b'grant_type=client_credentials', sent['body'])
        self.assertIn(b'scope=product.compact', sent['body'])
        self.assertNotIn('secret-1', json.dumps(sent['body'].decode()),
                         'the secret belongs in the Basic header, never the body')

    def test_a_refused_token_is_an_error_not_an_empty_string(self):
        fetch = Recorder([(401, {'error': 'invalid_client'})])
        with self.assertRaises(ValueError) as caught:
            refresh.kroger_token(fetch, 'id-1', 'bad')
        self.assertIn('401', str(caught.exception))


class KrogerPrices(unittest.TestCase):
    def test_locations_are_searched_by_zip(self):
        fetch = Recorder([(200, LOCATIONS_REPLY)])
        stores = refresh.kroger_locations(fetch, 'tok_abc', '90017', limit=3)
        self.assertEqual(stores[0]['location_id'], '70100123')
        self.assertEqual(stores[0]['name'], 'Ralphs Wilshire')
        self.assertEqual(stores[0]['zip'], '90017')
        sent = fetch.requests[0]
        self.assertIn('filter.zipCode.near=90017', sent['url'])
        self.assertIn('filter.limit=3', sent['url'])
        self.assertEqual(sent['headers']['Authorization'], 'Bearer tok_abc')

    def test_products_carry_price_promo_size_and_image(self):
        fetch = Recorder([(200, PRODUCTS_REPLY)])
        found = refresh.kroger_products(fetch, 'tok_abc', '70100123', 'milk', limit=5)
        milk, rice = found
        self.assertEqual(milk['product_id'], '0001111041600')
        self.assertEqual(milk['price'], 2.29)
        self.assertIsNone(milk['promo'], 'promo is 0 when nothing is on sale, not a price')
        self.assertEqual(milk['size'], '1/2 gal')
        self.assertEqual(milk['image'],
                         'https://www.kroger.com/product/images/medium/front/0001111041600')
        self.assertEqual(rice['promo'], 3.99)
        self.assertIsNone(rice['image'], 'no image is None, never a guessed URL')
        sent = fetch.requests[0]
        self.assertIn('filter.term=milk', sent['url'])
        self.assertIn('filter.locationId=70100123', sent['url'])

    def test_prices_are_gathered_per_ingredient_with_one_call_each(self):
        fetch = Recorder([(200, TOKEN_REPLY), (200, PRODUCTS_REPLY), (200, PRODUCTS_REPLY)])
        snapshot = refresh.fetch_prices(fetch, 'id-1', 'secret-1', '70100123', ['milk', 'rice'])
        self.assertEqual(sorted(snapshot['items']), ['milk', 'rice'])
        self.assertEqual(snapshot['location_id'], '70100123')
        self.assertEqual(len([r for r in fetch.requests if 'products' in r['url']]), 2)
        self.assertEqual(snapshot['items']['milk'][0]['price'], 2.29)


class SizeParsing(unittest.TestCase):
    def test_sizes_that_parse(self):
        self.assertEqual(refresh.parse_size('32 oz'), (32.0, 'oz'))
        self.assertEqual(refresh.parse_size('1/2 gal'), (0.5, 'gal'))
        self.assertEqual(refresh.parse_size('16 fl oz'), (16.0, 'fl oz'))
        self.assertEqual(refresh.parse_size('2 lb'), (2.0, 'lb'))

    def test_sizes_that_do_not_parse_are_admitted_not_guessed(self):
        for unparseable in ('family pack', '', None, 'per lb', '32 onz'):
            self.assertIsNone(refresh.parse_size(unparseable), unparseable)

    def test_counted_goods_parse_as_counts(self):
        # Eggs are sold "12 ct", avocados "1 each" — the priciest line in a real
        # basket was an estimate purely because counts were treated as unreadable.
        self.assertEqual(refresh.parse_size('12 ct'), (12.0, 'ct'))
        self.assertEqual(refresh.parse_size('18 ct'), (18.0, 'ct'))
        self.assertEqual(refresh.parse_size('1 each'), (1.0, 'ct'))
        self.assertEqual(refresh.parse_size('1 ea'), (1.0, 'ct'))

    def test_unit_price_per_count(self):
        self.assertEqual(refresh.unit_price(4.39, '18 ct'), (0.24, 'ct'))
        self.assertEqual(refresh.unit_price(1.5, '1 each'), (1.5, 'ct'))

    def test_unit_price_per_kilo_or_litre_else_none(self):
        self.assertAlmostEqual(refresh.unit_price(4.49, '32 oz')[0], 4.95, places=2)
        self.assertEqual(refresh.unit_price(4.49, '32 oz')[1], 'kg')
        self.assertAlmostEqual(refresh.unit_price(2.29, '1/2 gal')[0], 1.21, places=2)
        self.assertEqual(refresh.unit_price(2.29, '1/2 gal')[1], 'l')
        self.assertIsNone(refresh.unit_price(3.0, 'family pack'))


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_store_snapshot_names_its_source_and_licence(self):
        stores = refresh.store_records(OVERPASS_REPLY['elements'])
        path = self.folder / 'stores.json'
        refresh.write_snapshot(path, {'stores': stores}, source='OpenStreetMap via Overpass',
                               licence='ODbL 1.0')
        saved = json.loads(path.read_text())
        self.assertEqual(saved['source'], 'OpenStreetMap via Overpass')
        self.assertEqual(saved['licence'], 'ODbL 1.0')
        self.assertRegex(saved['captured'], r'^\d{4}-\d{2}-\d{2}')
        self.assertEqual(len(saved['stores']), 2)

    def test_price_snapshot_is_private_to_the_installation(self):
        path = self.folder / 'prices.70100123.json'
        refresh.write_snapshot(path, {'items': {}}, source='Kroger Products API',
                               licence='Kroger developer terms; not redistributed', private=True)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_snapshot_write_is_atomic(self):
        path = self.folder / 'stores.json'
        refresh.write_snapshot(path, {'stores': []}, source='s', licence='l')
        self.assertEqual([p.name for p in self.folder.iterdir()], ['stores.json'],
                         'no temporary file may be left beside the snapshot')


class Relevance(unittest.TestCase):
    """Kroger's search returns things that are not the ingredient. A plantain came
    back for "banana" and, being the only count-priced row, won the basket at $0.99
    each. A product has to name what it claims to be."""

    def test_a_product_must_name_the_ingredient(self):
        self.assertTrue(refresh.relevant('banana', 'banana',
                                         'Fresh Bunch of Bananas - 5-7 Bananas'))
        self.assertFalse(refresh.relevant('banana', 'banana', 'Fresh Plantain - Single'))

    def test_the_search_term_is_what_gets_matched_not_the_catalogue_name(self):
        # The catalogue says yoghurt; the shelf says Yogurt. The alias carries the
        # match, and "plain" is what keeps a blueberry dessert cup out of it.
        self.assertTrue(refresh.relevant('yoghurt', 'plain yogurt',
                                         'Kroger Plain Low Fat Yogurt Tub'))
        self.assertFalse(refresh.relevant('yoghurt', 'plain yogurt',
                                          'Noosa Blueberry Yogurt Cup'))

    def test_plurals_and_compound_names_still_match(self):
        self.assertTrue(refresh.relevant('berries', 'berries',
                                         'Fresh Strawberries - 1 LB Clamshell'))
        self.assertTrue(refresh.relevant('eggs', 'eggs',
                                         'Kroger Cage Free Grade AA Large White Eggs'))
        self.assertTrue(refresh.relevant('black beans', 'black beans',
                                         'Kroger Black Bean Each'))
        # "Rice Krispies Treats Bar" passes this check, and should: it names rice.
        # Telling a grain from a cereal bar needs category knowledge a name check
        # does not have, and pretending otherwise would be a test that lies.

    def test_synonyms_and_spacing_do_not_reject_the_right_product(self):
        # Live, the guard threw away every chickpea Ralphs sells: the shelf calls
        # them garbanzo beans, or spaces the word as "Chick Peas".
        self.assertTrue(refresh.relevant('chickpeas', 'chickpeas',
                                         'Kroger Garbanzo Beans Each'))
        self.assertTrue(refresh.relevant('chickpeas', 'chickpeas',
                                         'Goya Canned Chick Peas'))
        self.assertTrue(refresh.relevant('courgette', 'zucchini', 'Zucchini Tray'))
        self.assertFalse(refresh.relevant('chickpeas', 'chickpeas',
                                          'Fresh Plantain - Single'))

    def test_fetched_rows_carry_their_verdict(self):
        fetch = Recorder([(200, TOKEN_REPLY), (200, PRODUCTS_REPLY)])
        snapshot = refresh.fetch_prices(fetch, 'id-1', 'secret-1', '70100123', ['milk'])
        rows = snapshot['items']['milk']
        self.assertTrue(rows[0]['relevant'], rows[0]['description'])
        self.assertFalse(rows[1]['relevant'], 'the rice row came back for a milk search')


class TermAliases(unittest.TestCase):
    """The catalogue is written in British English; Kroger sells American groceries.
    The search term may differ from the catalogue name, but the snapshot key may not:
    the skill looks ingredients up by the name the recipe uses."""

    def test_a_catalogue_name_can_be_searched_under_another(self):
        fetch = Recorder([(200, TOKEN_REPLY), (200, PRODUCTS_REPLY)])
        snapshot = refresh.fetch_prices(fetch, 'id-1', 'secret-1', '70300022', ['courgette'])
        searched = [row for row in fetch.requests if '/products' in row['url']][0]
        self.assertIn('filter.term=zucchini', searched['url'])
        self.assertIn('courgette', snapshot['items'])
        self.assertNotIn('zucchini', snapshot['items'])

    def test_an_unaliased_name_is_searched_as_written(self):
        fetch = Recorder([(200, TOKEN_REPLY), (200, PRODUCTS_REPLY)])
        refresh.fetch_prices(fetch, 'id-1', 'secret-1', '70300022', ['rice'])
        searched = [row for row in fetch.requests if '/products' in row['url']][0]
        self.assertIn('filter.term=rice', searched['url'])

    def test_known_aliases(self):
        self.assertEqual(refresh.search_term('courgette'), 'zucchini')
        self.assertEqual(refresh.search_term('yoghurt'), 'plain yogurt')
        self.assertEqual(refresh.search_term('rice'), 'rice')


if __name__ == '__main__':
    unittest.main()
