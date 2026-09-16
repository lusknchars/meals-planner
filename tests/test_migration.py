"""Contract for opening a database written by an older build.

Every other test starts from an empty file, so none of them noticed that
CREATE TABLE IF NOT EXISTS never alters a table that already exists. A real
installation had talked to the agent before today's columns were added, and its
next `profile set` failed with "table profiles has no column named
store_location_id" -- a hard failure for anyone who had already used it.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'

# profiles exactly as the first shipped build created it.
ORIGINAL_SCHEMA = '''
CREATE TABLE profiles (
  scope TEXT PRIMARY KEY, people INTEGER NOT NULL, calories INTEGER NOT NULL,
  budget REAL, diet TEXT NOT NULL, lat REAL, lon REAL, address TEXT,
  currency TEXT NOT NULL);
CREATE TABLE plans (
  scope TEXT NOT NULL, start TEXT NOT NULL, days INTEGER NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY (scope, start));
'''

ADDED_SINCE = ('store_location_id', 'age', 'sex', 'height_in', 'weight_lb', 'activity', 'goal')


class OlderDatabase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        folder = self.home / 'meals'
        folder.mkdir()
        self.store = folder / 'meals.sqlite3'
        db = sqlite3.connect(self.store)
        db.executescript(ORIGINAL_SCHEMA)
        db.execute("INSERT INTO profiles (scope, people, calories, budget, diet, lat, lon,"
                   " address, currency) VALUES ('chat-1', 2, 2100, 140, '[\"vegetarian\"]',"
                   " 34.05, -118.24, NULL, 'USD')")
        db.commit()
        db.close()
        self.catalogue = self.home / 'catalogue.json'
        self.catalogue.write_text(json.dumps({
            'currency': 'USD', 'venues': [],
            'recipes': [{'id': 'oats', 'title': 'Oats', 'slot': 'breakfast', 'calories': 400,
                         'cost': 2.0, 'tags': ['vegetarian'],
                         'ingredients': [{'item': 'oats', 'quantity': 100, 'unit': 'g',
                                          'cost': 2.0}]}]}))

    def call(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', *args],
                                env={**os.environ, 'HERMES_HOME': str(self.home),
                                     'MEALS_CATALOGUE': str(self.catalogue)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def columns(self):
        db = sqlite3.connect(self.store)
        try:
            return [row[1] for row in db.execute('PRAGMA table_info(profiles)')]
        finally:
            db.close()

    def test_the_missing_columns_are_added_on_open(self):
        self.assertNotIn('store_location_id', self.columns(), 'fixture must start old')
        self.call('profile', 'show')
        for column in ADDED_SINCE:
            self.assertIn(column, self.columns(), column)

    def test_a_profile_written_by_the_older_build_survives(self):
        saved = self.call('profile', 'show')['profile']
        self.assertEqual(saved['people'], 2)
        self.assertEqual(saved['calories'], 2100)
        self.assertEqual(saved['diet'], ['vegetarian'])
        self.assertIsNone(saved['store_location_id'], 'a new column starts empty, not absent')

    def test_setting_a_new_field_works_against_an_old_database(self):
        # The exact failure seen live: "table profiles has no column named
        # store_location_id".
        self.call('profile', 'set', '--store', '70300022', '--age', '30', '--sex', 'male',
                  '--height-in', '70', '--weight-lb', '175', '--activity', 'moderate',
                  '--goal', 'maintenance')
        saved = self.call('profile', 'show')['profile']
        self.assertEqual(saved['store_location_id'], '70300022')
        self.assertEqual(saved['age'], 30)
        targets = self.call('targets')
        self.assertGreater(targets['calories'], 0)

    def test_opening_twice_does_not_add_a_column_twice(self):
        self.call('profile', 'show')
        self.call('profile', 'show')
        columns = self.columns()
        self.assertEqual(len(columns), len(set(columns)))


if __name__ == '__main__':
    unittest.main()
