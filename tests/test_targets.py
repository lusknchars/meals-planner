"""Contract for daily targets: the arithmetic a person's plan rests on.

Mifflin-St Jeor, an activity multiplier, a goal adjustment, then macro grams.
All of it deterministic, so it lives in the script and is checked here rather
than trusted to a model that is good at sounding certain about numbers.

The floors matter most: 1,200 kcal for women and 1,500 for men are enforced in
code, not asked for politely in a prompt.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/meals/scripts/meals.py'
_spec = importlib.util.spec_from_file_location('meals_module', SCRIPT)
meals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(meals)


class Energy(unittest.TestCase):
    def test_mifflin_st_jeor_for_a_man(self):
        # 30 years, 5'10", 175 lb -> 79.38 kg, 177.8 cm
        # 10(79.38) + 6.25(177.8) - 5(30) + 5 = 1760
        self.assertAlmostEqual(meals.bmr('male', 175, 70, 30), 1760, delta=2)

    def test_mifflin_st_jeor_for_a_woman(self):
        # 35 years, 5'5", 150 lb -> 68.04 kg, 165.1 cm
        # 10(68.04) + 6.25(165.1) - 5(35) - 161 = 1376
        self.assertAlmostEqual(meals.bmr('female', 150, 65, 35), 1376, delta=2)

    def test_activity_multiplies_the_resting_rate(self):
        self.assertAlmostEqual(meals.tdee(1760, 'moderate'), 2728, delta=2)
        self.assertAlmostEqual(meals.tdee(1760, 'sedentary'), 2112, delta=2)
        self.assertAlmostEqual(meals.tdee(1760, 'very active'), 3036, delta=2)

    def test_an_unknown_activity_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            meals.tdee(1760, 'quite busy')


class Targets(unittest.TestCase):
    def test_goals_move_the_target_in_the_expected_direction(self):
        maintain, _ = meals.target_calories(2728, 'maintenance', 'male')
        loss, _ = meals.target_calories(2728, 'fat loss', 'male')
        gain, _ = meals.target_calories(2728, 'muscle gain', 'male')
        self.assertEqual(maintain, 2728)
        self.assertLess(loss, maintain)
        self.assertGreater(gain, maintain)
        self.assertAlmostEqual(loss, 2182, delta=2)   # -20%

    def test_the_floor_holds_for_a_woman(self):
        # 60 years, 5'0", 110 lb, sedentary: a 20% cut lands near 950 kcal.
        resting = meals.bmr('female', 110, 60, 60)
        daily = meals.tdee(resting, 'sedentary')
        target, floored = meals.target_calories(daily, 'fat loss', 'female')
        self.assertEqual(target, 1200)
        self.assertTrue(floored, 'the clamp must be reported, not applied silently')

    def test_the_floor_holds_for_a_man(self):
        target, floored = meals.target_calories(1600, 'fat loss', 'male')
        self.assertEqual(target, 1500)
        self.assertTrue(floored)

    def test_a_target_above_the_floor_is_not_marked_floored(self):
        target, floored = meals.target_calories(2728, 'fat loss', 'male')
        self.assertFalse(floored)
        self.assertGreater(target, 1500)


class Macros(unittest.TestCase):
    def test_grams_reconcile_to_the_calorie_target(self):
        split = meals.macros(2728, 175, 'maintenance')
        energy = split['protein_g'] * 4 + split['carb_g'] * 4 + split['fat_g'] * 9
        self.assertAlmostEqual(energy, 2728, delta=12, msg=split)

    def test_protein_follows_the_goal_within_the_evidence_range(self):
        for goal in ('fat loss', 'maintenance', 'muscle gain', 'performance'):
            split = meals.macros(2400, 175, goal)
            per_lb = split['protein_g'] / 175
            self.assertGreaterEqual(per_lb, 0.7, goal)
            self.assertLessEqual(per_lb, 1.0, goal)
        self.assertGreater(meals.macros(2400, 175, 'muscle gain')['protein_g'],
                           meals.macros(2400, 175, 'maintenance')['protein_g'])

    def test_fat_stays_inside_twenty_to_thirty_five_percent(self):
        split = meals.macros(2728, 175, 'maintenance')
        share = split['fat_g'] * 9 / 2728
        self.assertGreaterEqual(share, 0.20)
        self.assertLessEqual(share, 0.35)

    def test_fibre_follows_intake_with_a_floor(self):
        self.assertEqual(meals.macros(2728, 175, 'maintenance')['fiber_g'], 38)  # 14g/1000
        self.assertEqual(meals.macros(1500, 120, 'fat loss')['fiber_g'], 25)     # floor

    def test_a_target_too_small_for_its_protein_is_refused(self):
        # Protein and fat alone would exceed the target: report it, never hand
        # back negative carbohydrates.
        with self.assertRaises(ValueError):
            meals.macros(900, 220, 'muscle gain')


class TargetsCommand(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.catalogue = self.home / 'catalogue.json'
        # One recipe, because an empty catalogue is refused on load and every
        # command reads it — even one that only does arithmetic on the profile.
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

    def test_stats_on_the_profile_produce_a_full_target_set(self):
        self.call('profile', 'set', '--people', '1', '--age', '30', '--sex', 'male',
                  '--height-in', '70', '--weight-lb', '175', '--activity', 'moderate',
                  '--goal', 'maintenance')
        targets = self.call('targets')
        self.assertAlmostEqual(targets['calories'], 2728, delta=3)
        self.assertAlmostEqual(targets['bmr'], 1760, delta=3)
        self.assertAlmostEqual(targets['tdee'], 2728, delta=3)
        self.assertEqual(targets['goal'], 'maintenance')
        self.assertFalse(targets['floored'])
        for key in ('protein_g', 'carb_g', 'fat_g', 'fiber_g', 'water_oz'):
            self.assertGreater(targets[key], 0, key)

    def test_targets_without_stats_say_what_is_missing(self):
        self.call('profile', 'set', '--people', '1', '--calories', '2000')
        error = self.call('targets', success=False)
        for missing in ('age', 'sex', 'height', 'weight'):
            self.assertIn(missing, error['error'].lower())

    def test_a_clamped_target_says_so_in_the_result(self):
        self.call('profile', 'set', '--people', '1', '--age', '60', '--sex', 'female',
                  '--height-in', '60', '--weight-lb', '110', '--activity', 'sedentary',
                  '--goal', 'fat loss')
        targets = self.call('targets')
        self.assertEqual(targets['calories'], 1200)
        self.assertTrue(targets['floored'])
        self.assertIn('1,200', targets['note'])


if __name__ == '__main__':
    unittest.main()
