"""Contract for the USDA nutrition snapshot.

Per-100 g macros for every catalogue ingredient, plus the published gram weight
of a portion — which is what lets a recipe that counts bananas report macros
without anybody inventing what a banana weighs.

No network here: fetch is injected, and the payloads mirror real responses seen
on 2026-09-16 from FoodData Central.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import refresh  # noqa: E402


class Recorder:
    """Stands in for refresh's HTTP call. Defined here rather than imported from
    the sibling test module: that only resolves under discovery, and test files
    importing each other is a coupling that breaks when one is run alone."""

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


SEARCH_REPLY = {'foods': [
    {'fdcId': 173944, 'description': 'Bananas, raw', 'dataType': 'SR Legacy'},
    {'fdcId': 999999, 'description': 'Bananas, dehydrated', 'dataType': 'SR Legacy'},
]}

DETAIL_REPLY = {
    'fdcId': 173944, 'description': 'Bananas, raw', 'dataType': 'SR Legacy',
    'foodNutrients': [
        # Energy is published twice, in kcal and in kJ. Matching on the name
        # alone records 371 calories for a banana.
        {'nutrient': {'name': 'Energy', 'unitName': 'kcal'}, 'amount': 89.0},
        {'nutrient': {'name': 'Energy', 'unitName': 'kJ'}, 'amount': 371.0},
        {'nutrient': {'name': 'Protein', 'unitName': 'g'}, 'amount': 1.09},
        {'nutrient': {'name': 'Total lipid (fat)', 'unitName': 'g'}, 'amount': 0.33},
        {'nutrient': {'name': 'Carbohydrate, by difference', 'unitName': 'g'}, 'amount': 22.84},
        {'nutrient': {'name': 'Fiber, total dietary', 'unitName': 'g'}, 'amount': 2.6},
    ],
    'foodPortions': [
        {'modifier': 'NLEA serving', 'amount': 1.0, 'gramWeight': 126.0},
        {'modifier': 'large (8" to 8-7/8" long)', 'amount': 1.0, 'gramWeight': 136.0},
    ],
}

NO_FIBRE_REPLY = {
    'fdcId': 100, 'description': 'Oil, olive, salad or cooking', 'dataType': 'SR Legacy',
    'foodNutrients': [
        {'nutrient': {'name': 'Energy', 'unitName': 'kcal'}, 'amount': 884.0},
        {'nutrient': {'name': 'Total lipid (fat)', 'unitName': 'g'}, 'amount': 100.0},
    ],
    'foodPortions': [],
}


class Key(unittest.TestCase):
    """The Kroger path reads .env; this one only read the environment, so a key
    written to .env was silently ignored and DEMO_KEY's spent quota was used
    instead. Nothing covered it, which is why it shipped."""

    def setUp(self):
        self.saved = os.environ.pop('USDA_API_KEY', None)
        self.addCleanup(lambda: os.environ.__setitem__('USDA_API_KEY', self.saved)
                        if self.saved is not None else None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = Path(self.temp.name) / '.env'

    def test_the_key_comes_from_the_env_file(self):
        self.env.write_text('KROGER_CLIENT_ID=x\nUSDA_API_KEY=abc123\n')
        self.assertEqual(refresh.usda_key(self.env), 'abc123')

    def test_quotes_and_spacing_do_not_travel_with_it(self):
        self.env.write_text('USDA_API_KEY = "abc123"\n')
        self.assertEqual(refresh.usda_key(self.env), 'abc123')

    def test_the_environment_wins_over_the_file(self):
        self.env.write_text('USDA_API_KEY=from-file\n')
        os.environ['USDA_API_KEY'] = 'from-environment'
        self.addCleanup(os.environ.pop, 'USDA_API_KEY', None)
        self.assertEqual(refresh.usda_key(self.env), 'from-environment')

    def test_demo_key_is_only_the_last_resort(self):
        self.assertEqual(refresh.usda_key(Path(self.temp.name) / 'absent.env'), 'DEMO_KEY')


class Lookup(unittest.TestCase):
    def test_search_asks_for_reference_foods_by_name(self):
        fetch = Recorder([(200, SEARCH_REPLY)])
        found = refresh.usda_search(fetch, 'KEY123', 'bananas raw')
        self.assertEqual(found['fdc_id'], 173944)
        self.assertEqual(found['description'], 'Bananas, raw')
        sent = fetch.requests[0]
        self.assertIn('api_key=KEY123', sent['url'])
        self.assertIn('bananas', sent['url'])
        self.assertIn('Foundation', sent['url'],
                      'reference data carries portions; branded rows usually do not')

    def test_nothing_found_is_none_not_an_exception(self):
        fetch = Recorder([(200, {'foods': []})])
        self.assertIsNone(refresh.usda_search(fetch, 'KEY123', 'unobtainium'))


class Choosing(unittest.TestCase):
    """USDA returns what it returns, and its first row is often a derivative.

    Live, taking the top hit gave "Oil, oat" for oats (884 kcal, pure fat),
    "Bacon, meatless" for bacon and "Bananas, dehydrated" for bananas. Wrong
    macros feed a diet, so an ingredient with no acceptable row must come back
    unmatched rather than approximately right.
    """

    OATS = [{'fdcId': 1, 'dataType': 'SR Legacy', 'description': 'Oil, oat'},
            {'fdcId': 2, 'dataType': 'SR Legacy', 'description': 'Bagels, oat bran'},
            {'fdcId': 3, 'dataType': 'SR Legacy', 'description': 'Bread, oat bran'}]
    EGGS = [{'fdcId': 10, 'dataType': 'Foundation',
             'description': 'Eggs, Grade A, Large, egg white'},
            {'fdcId': 11, 'dataType': 'Foundation',
             'description': 'Eggs, Grade A, Large, egg whole'},
            {'fdcId': 12, 'dataType': 'Foundation',
             'description': 'Eggs, Grade A, Large, egg yolk'}]
    BANANA = [{'fdcId': 20, 'dataType': 'SR Legacy',
               'description': 'Bananas, dehydrated, or banana powder'},
              {'fdcId': 21, 'dataType': 'SR Legacy', 'description': 'Bananas, raw'},
              {'fdcId': 22, 'dataType': 'SR Legacy', 'description': 'Snacks, banana chips'}]
    SALMON = [{'fdcId': 30, 'dataType': 'SR Legacy', 'description': 'Vegetarian fillets'},
              {'fdcId': 31, 'dataType': 'SR Legacy', 'description': 'Fish oil, salmon'},
              {'fdcId': 32, 'dataType': 'SR Legacy', 'description': 'Fish, salmon, chinook, raw'}]

    def test_nothing_acceptable_is_unmatched_not_approximately_right(self):
        self.assertIsNone(refresh.best_food(self.OATS, 'oats', 'oats'),
                          'oat oil is not oats at any confidence')

    def test_the_whole_food_beats_its_parts(self):
        chosen = refresh.best_food(self.EGGS, 'eggs', 'eggs')
        self.assertEqual(chosen['description'], 'Eggs, Grade A, Large, egg whole')

    def test_raw_beats_dehydrated_and_snacks(self):
        chosen = refresh.best_food(self.BANANA, 'banana', 'banana')
        self.assertEqual(chosen['description'], 'Bananas, raw')

    def test_the_real_fish_beats_a_vegetarian_fillet_and_an_oil(self):
        chosen = refresh.best_food(self.SALMON, 'salmon fillet', 'salmon fillet')
        self.assertEqual(chosen['description'], 'Fish, salmon, chinook, raw')

    def test_foundation_wins_a_tie(self):
        rows = [{'fdcId': 40, 'dataType': 'SR Legacy', 'description': 'Spinach, raw'},
                {'fdcId': 41, 'dataType': 'Foundation', 'description': 'Spinach, raw'}]
        self.assertEqual(refresh.best_food(rows, 'spinach', 'spinach')['fdcId'], 41)

    def test_a_reject_word_inside_another_word_does_not_fire(self):
        # "oil" is a reject word and "boiled" contains it, so a substring test
        # threw away every cooked food USDA publishes: chickpeas, asparagus,
        # beets. Three curated queries returned nothing at all because of it.
        cooked = [{'fdcId': 100, 'dataType': 'SR Legacy',
                   'description': 'Chickpeas (garbanzo beans, bengal gram), mature seeds, '
                                  'cooked, boiled, without salt'}]
        self.assertIsNotNone(refresh.best_food(cooked, 'chickpeas', 'chickpeas cooked boiled'))
        # The actual oil is still refused for an ingredient that is not oil.
        self.assertIsNone(refresh.best_food(
            [{'fdcId': 101, 'dataType': 'SR Legacy', 'description': 'Oil, oat'}], 'oats', 'oats'))

    def test_a_form_word_that_is_the_ingredient_cannot_reject_it(self):
        # The reject list contains oil, bread and sauce, which left olive oil,
        # bread and soy sauce unmatched: the filter threw away the ingredients
        # named after the forms it was meant to exclude.
        oil = [{'fdcId': 50, 'dataType': 'SR Legacy',
                'description': 'Oil, olive, salad or cooking'}]
        self.assertIsNotNone(refresh.best_food(oil, 'olive oil', 'olive oil'))
        loaf = [{'fdcId': 51, 'dataType': 'SR Legacy', 'description': 'Bread, whole-wheat'}]
        self.assertIsNotNone(refresh.best_food(loaf, 'bread', 'bread'))
        shoyu = [{'fdcId': 52, 'dataType': 'SR Legacy',
                  'description': 'Soy sauce made from soy and wheat (shoyu)'}]
        self.assertIsNotNone(refresh.best_food(shoyu, 'soy sauce', 'soy sauce'))

    def test_every_distinctive_word_must_appear(self):
        plum = [{'fdcId': 60, 'dataType': 'SR Legacy',
                 'description': 'Plum, black, with skin, raw'}]
        self.assertIsNone(refresh.best_food(plum, 'black beans', 'black beans'),
                          'matching "black" alone accepted a plum for black beans')
        beans = [{'fdcId': 61, 'dataType': 'SR Legacy',
                  'description': 'Beans, black, mature seeds, raw'}]
        self.assertIsNotNone(refresh.best_food(beans, 'black beans', 'black beans'))

    def test_a_generic_cut_word_does_not_block_the_food(self):
        salmon = [{'fdcId': 62, 'dataType': 'Foundation',
                   'description': 'Fish, salmon, sockeye, wild caught, raw'}]
        self.assertIsNotNone(refresh.best_food(salmon, 'salmon fillet', 'salmon fillet'))

    def test_the_curated_query_words_break_a_tie(self):
        rows = [{'fdcId': 70, 'dataType': 'SR Legacy',
                 'description': 'Bacon, turkey, unprepared'},
                {'fdcId': 71, 'dataType': 'SR Legacy',
                 'description': 'Pork, cured, bacon, unprepared'}]
        chosen = refresh.best_food(rows, 'bacon', 'pork cured bacon unprepared')
        self.assertEqual(chosen['fdcId'], 71, 'the curated query asked for pork')

    def test_atwater_energy_counts_as_energy(self):
        detail = {'fdcId': 80, 'description': 'Oats, whole grain, steel cut',
                  'foodNutrients': [
                      {'nutrient': {'name': 'Energy (Atwater General Factors)',
                                    'unitName': 'kcal'}, 'amount': 381.248},
                      {'nutrient': {'name': 'Energy (Atwater Specific Factors)',
                                    'unitName': 'kcal'}, 'amount': 379.2080612},
                      {'nutrient': {'name': 'Protein', 'unitName': 'g'}, 'amount': 12.51},
                  ], 'foodPortions': []}
        row = refresh.nutrition_record(detail)
        self.assertAlmostEqual(row['per_100g']['kcal'], 379.2, delta=0.1)
        self.assertEqual(row['per_100g']['energy_source'], 'Atwater specific factors')

    def test_a_curated_query_replaces_the_search_term(self):
        # Neither plain oats nor cured bacon appears in a bare search's top rows,
        # so those two ingredients name the query that finds them.
        self.assertIn('oats', refresh.usda_query('oats').lower())
        self.assertNotEqual(refresh.usda_query('oats'), 'oats')
        self.assertIn('pork', refresh.usda_query('bacon').lower())
        self.assertEqual(refresh.usda_query('spinach'), 'spinach')


class Record(unittest.TestCase):
    def test_macros_come_back_per_hundred_grams(self):
        row = refresh.nutrition_record(DETAIL_REPLY)
        self.assertEqual(row['per_100g']['kcal'], 89.0, 'the kJ figure must not win')
        self.assertEqual(row['per_100g']['protein_g'], 1.09)
        self.assertEqual(row['per_100g']['carb_g'], 22.84)
        self.assertEqual(row['per_100g']['fat_g'], 0.33)
        self.assertEqual(row['per_100g']['fiber_g'], 2.6)
        self.assertEqual(row['description'], 'Bananas, raw')
        self.assertEqual(row['fdc_id'], 173944)

    def test_portion_weights_are_kept_with_their_labels(self):
        row = refresh.nutrition_record(DETAIL_REPLY)
        self.assertEqual(row['portions'][0], {'label': 'NLEA serving', 'grams': 126.0})
        self.assertEqual(row['serving_g'], 126.0, 'the NLEA serving is the default portion')
        self.assertEqual(len(row['portions']), 2)

    def test_energy_in_kilojoules_alone_is_converted(self):
        # Some SR Legacy rows publish energy only in kJ, which left a correctly
        # matched yogurt with kcal: None.
        detail = {'fdcId': 5, 'description': 'Yogurt, plain, nonfat', 'foodNutrients': [
            {'nutrient': {'name': 'Energy', 'unitName': 'kJ'}, 'amount': 234.0},
            {'nutrient': {'name': 'Protein', 'unitName': 'g'}, 'amount': 5.25},
            {'nutrient': {'name': 'Carbohydrate, by difference', 'unitName': 'g'},
             'amount': 7.68},
            {'nutrient': {'name': 'Total lipid (fat)', 'unitName': 'g'}, 'amount': 0.18},
            {'nutrient': {'name': 'Fiber, total dietary', 'unitName': 'g'}, 'amount': 0.0},
        ], 'foodPortions': []}
        row = refresh.nutrition_record(detail)
        self.assertAlmostEqual(row['per_100g']['kcal'], 55.9, delta=0.2)
        self.assertEqual(row['per_100g']['energy_source'], 'converted from kJ')

    def test_foundation_fibre_is_read_too(self):
        # Foundation rows publish "Total dietary fiber (AOAC 2011.25)"; SR Legacy
        # publishes "Fiber, total dietary". Matching one name left most foods
        # reporting no fibre, which a fibre target cannot survive.
        detail = {'fdcId': 90, 'description': 'Oats, whole grain, rolled', 'foodNutrients': [
            {'nutrient': {'name': 'Energy (Atwater Specific Factors)', 'unitName': 'kcal'},
             'amount': 378.9},
            {'nutrient': {'name': 'Total dietary fiber (AOAC 2011.25)', 'unitName': 'g'},
             'amount': 10.1},
            {'nutrient': {'name': 'High Molecular Weight Dietary Fiber (HMWDF)',
                          'unitName': 'g'}, 'amount': 9.4},
        ], 'foodPortions': []}
        row = refresh.nutrition_record(detail)
        self.assertAlmostEqual(row['per_100g']['fiber_g'], 10.1, delta=0.1)

    def test_a_missing_nutrient_is_unknown_not_zero(self):
        row = refresh.nutrition_record(NO_FIBRE_REPLY)
        self.assertEqual(row['per_100g']['fat_g'], 100.0)
        self.assertIsNone(row['per_100g']['fiber_g'])
        self.assertIsNone(row['per_100g']['protein_g'])
        self.assertIsNone(row['serving_g'], 'no published portion means no serving weight')


class Snapshot(unittest.TestCase):
    def test_terms_are_searched_by_alias_and_keyed_by_the_catalogue_name(self):
        fetch = Recorder([(200, SEARCH_REPLY), (200, DETAIL_REPLY)])
        snapshot = refresh.fetch_nutrition(fetch, 'KEY123', ['courgette'])
        searched = fetch.requests[0]['url']
        self.assertIn('zucchini', searched)
        self.assertIn('courgette', snapshot['items'])
        self.assertNotIn('zucchini', snapshot['items'])

    def test_an_ingredient_with_no_match_is_recorded_as_unknown(self):
        fetch = Recorder([(200, {'foods': []})])
        snapshot = refresh.fetch_nutrition(fetch, 'KEY123', ['unobtainium'])
        self.assertIsNone(snapshot['items']['unobtainium'])
        self.assertEqual(snapshot['unmatched'], ['unobtainium'])

    def test_one_search_and_one_detail_call_per_ingredient(self):
        fetch = Recorder([(200, SEARCH_REPLY), (200, DETAIL_REPLY)])
        refresh.fetch_nutrition(fetch, 'KEY123', ['banana'])
        self.assertEqual(len(fetch.requests), 2)
        self.assertIn('/foods/search', fetch.requests[0]['url'])
        self.assertIn('/food/173944', fetch.requests[1]['url'])

    def test_a_refusal_part_way_keeps_what_was_already_fetched(self):
        # DEMO_KEY allows about thirty calls an hour and a catalogue needs more,
        # so a rate limit mid-run must not discard the lookups that succeeded.
        fetch = Recorder([(200, SEARCH_REPLY), (200, DETAIL_REPLY),
                          (429, {'error': {'code': 'OVER_RATE_LIMIT'}})])
        snapshot = refresh.fetch_nutrition(fetch, 'KEY123', ['banana', 'oats', 'rice'])
        self.assertIsNotNone(snapshot['items']['banana'])
        self.assertEqual(snapshot['pending'], ['oats', 'rice'])
        self.assertNotIn('oats', snapshot['items'])
        self.assertIn('OVER_RATE_LIMIT', snapshot['stopped'])

    def test_a_transport_failure_also_keeps_what_was_fetched(self):
        # A rate limit was handled; an SSL handshake timeout was not, and it
        # discarded a run's worth of completed lookups.
        class Flaky:
            def __init__(self):
                self.calls = 0

            def __call__(self, method, url, headers=None, body=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    return 200, json.dumps(SEARCH_REPLY).encode()
                if self.calls == 2:
                    return 200, json.dumps(DETAIL_REPLY).encode()
                raise urllib.error.URLError('_ssl.c:983: The handshake operation timed out')

        snapshot = refresh.fetch_nutrition(Flaky(), 'KEY123', ['banana', 'oats', 'rice'])
        self.assertIsNotNone(snapshot['items']['banana'])
        self.assertEqual(snapshot['pending'], ['oats', 'rice'])
        self.assertIn('handshake', snapshot['stopped'])

    def test_a_complete_run_reports_nothing_pending(self):
        fetch = Recorder([(200, SEARCH_REPLY), (200, DETAIL_REPLY)])
        snapshot = refresh.fetch_nutrition(fetch, 'KEY123', ['banana'])
        self.assertEqual(snapshot['pending'], [])
        self.assertIsNone(snapshot['stopped'])

    def test_the_snapshot_names_its_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nutrition.json'
            refresh.write_snapshot(path, {'items': {}}, source='USDA FoodData Central',
                                   licence='public domain (US government work)')
            saved = json.loads(path.read_text())
            self.assertEqual(saved['source'], 'USDA FoodData Central')
            self.assertIn('public domain', saved['licence'])


if __name__ == '__main__':
    unittest.main()
