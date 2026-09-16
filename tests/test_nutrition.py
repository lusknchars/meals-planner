"""Contract for the USDA nutrition snapshot.

Per-100 g macros for every catalogue ingredient, plus the published gram weight
of a portion — which is what lets a recipe that counts bananas report macros
without anybody inventing what a banana weighs.

No network here: fetch is injected, and the payloads mirror real responses seen
on 2026-09-16 from FoodData Central.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

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
