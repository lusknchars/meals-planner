#!/usr/bin/env python3
"""Conversation-scoped meal plans, order drafts and calorie records.

Standard library only, no network. The JSON this prints is the only receipt that
something was saved; a non-zero exit means nothing was written.
"""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

SLOTS = ('breakfast', 'lunch', 'dinner')
EARTH_KM = 6371.0
# A far restaurant is worse than a dear one, and a meal that blows the day's
# remaining calories is worse than both. These weights are the whole ranking.
DISTANCE_WEIGHT = 0.5
OVERSHOOT_PENALTY = 6.0
# Recipe units on the left, snapshot unit-price units on the right. A pair that is
# not here (a count, a slice) has no comparable rate, so the estimate stands.
UNIT_SCALE = {('g', 'kg'): 0.001, ('kg', 'kg'): 1.0, ('ml', 'l'): 0.001, ('l', 'l'): 1.0,
              ('un', 'ct'): 1.0, ('sl', 'ct'): 1.0}

# Daily targets. Mifflin-St Jeor, a standard activity multiplier, a goal shift,
# then macro grams. Deterministic on purpose: a model is good at sounding certain
# about numbers, which is the wrong skill for the arithmetic a diet rests on.
ACTIVITY = {'sedentary': 1.2, 'light': 1.375, 'lightly active': 1.375,
            'moderate': 1.55, 'moderately active': 1.55, 'active': 1.55,
            'very': 1.725, 'very active': 1.725, 'extra active': 1.9, 'athlete': 1.9}
GOAL_SHIFT = {'fat loss': -0.20, 'maintenance': 0.0, 'maintain': 0.0,
              'muscle gain': 0.10, 'performance': 0.15}
PROTEIN_PER_LB = {'fat loss': 1.0, 'maintenance': 0.8, 'maintain': 0.8,
                  'muscle gain': 1.0, 'performance': 0.9}
FAT_SHARE = 0.25
# Enforced here rather than asked for in a prompt. Below these, a plan needs a
# clinician, not an agent.
CALORIE_FLOOR = {'female': 1200, 'male': 1500}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS profiles (
  scope TEXT PRIMARY KEY, people INTEGER NOT NULL, calories INTEGER NOT NULL,
  budget REAL, diet TEXT NOT NULL, lat REAL, lon REAL, address TEXT,
  currency TEXT NOT NULL, store_location_id TEXT, age INTEGER, sex TEXT,
  height_in REAL, weight_lb REAL, activity TEXT, goal TEXT, phone TEXT, country TEXT,
  price_country TEXT);
CREATE TABLE IF NOT EXISTS plans (
  scope TEXT NOT NULL, start TEXT NOT NULL, days INTEGER NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY (scope, start));
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, date TEXT NOT NULL,
  status TEXT NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, date TEXT NOT NULL,
  title TEXT NOT NULL, calories INTEGER NOT NULL, order_id TEXT);
CREATE TABLE IF NOT EXISTS overrides (
  scope TEXT NOT NULL, item TEXT NOT NULL, price REAL NOT NULL, unit TEXT NOT NULL,
  source TEXT, captured TEXT NOT NULL, PRIMARY KEY (scope, item));
'''


def text(value, name, maximum=200, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{name} must be nonempty text, at most {maximum} characters')
    return value.strip()


def day_of(value, name='date'):
    if value is None:
        return dt.date.today()
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be YYYY-MM-DD')
    return parsed


def positive(value, name):
    if value is None or value <= 0:
        raise ValueError(f'{name} must be greater than zero')
    return value


def catalogue():
    """Recipes and sample venues. MEALS_CATALOGUE replaces the shipped file."""
    path = Path(os.environ.get('MEALS_CATALOGUE')
                or Path(__file__).resolve().parents[1] / 'catalogue.json')
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'cannot read the catalogue at {path}: {error}')
    if not data.get('recipes'):
        raise ValueError(f'the catalogue at {path} has no recipes')
    data.setdefault('venues', [])
    data.setdefault('currency', 'USD')
    return data


def connect():
    home = os.environ.get('HERMES_HOME')
    if not home:
        raise ValueError('HERMES_HOME must name this agent installation')
    folder = Path(home) / 'meals'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = folder / 'meals.sqlite3'
    db = sqlite3.connect(store, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    migrate(db)
    os.chmod(store, 0o600)
    return db


# Columns added to profiles after the first build. CREATE TABLE IF NOT EXISTS
# never alters a table that already exists, so an installation that had talked to
# the agent before these arrived failed its next profile set with "table profiles
# has no column named store_location_id".
ADDED_COLUMNS = (('store_location_id', 'TEXT'), ('age', 'INTEGER'), ('sex', 'TEXT'),
                 ('height_in', 'REAL'), ('weight_lb', 'REAL'), ('activity', 'TEXT'),
                 ('goal', 'TEXT'), ('phone', 'TEXT'), ('country', 'TEXT'),
                 ('price_country', 'TEXT'))


def migrate(db):
    """Add any profile column this build expects and an older file lacks.

    Additive only: a new column starts empty, so a profile written by an earlier
    build keeps everything it had. Safe to run on every open.
    """
    present = {row[1] for row in db.execute('PRAGMA table_info(profiles)')}
    for column, kind in ADDED_COLUMNS:
        if column not in present:
            db.execute(f'ALTER TABLE profiles ADD COLUMN {column} {kind}')
    db.commit()


def suits(tags, diet):
    """Every restriction the person stated has to be met by the food."""
    return all(restriction in tags for restriction in diet)


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    half = (math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_KM * math.asin(math.sqrt(half))


def distance_km(profile, venue):
    if profile['lat'] is None or profile['lon'] is None:
        raise ValueError('set your location first: profile set --lat <n> --lon <n>')
    return round(haversine_km(profile['lat'], profile['lon'], venue['lat'], venue['lon']), 2)


def catalogue_dir():
    return Path(os.environ.get('MEALS_CATALOGUE')
                or Path(__file__).resolve().parents[1] / 'catalogue.json').parent


def read_snapshot(path, what):
    """A snapshot built out of band by refresh.py. Missing is a plain answer, not a crash."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'no usable {what} snapshot at {path} ({error.__class__.__name__}); '
                         f'build one with refresh.py')


def stores_snapshot():
    return read_snapshot(os.environ.get('MEALS_STORES') or catalogue_dir() / 'stores.json',
                         'stores')


def prices_snapshot(location_id):
    """This store's prices, or None. No snapshot simply means catalogue estimates."""
    if not location_id:
        return None
    folder = Path(os.environ.get('MEALS_PRICES_DIR') or catalogue_dir())
    path = folder / f'prices.{location_id}.json'
    return read_snapshot(path, 'prices') if path.is_file() else None


def value_label(rate, rates):
    """Name how good this price is against what the same ingredient costs here.

    The chosen row is always the cheapest, so the comparison says how far below
    the usual price it sits — not whether it beats some national average, which
    this data cannot support. One candidate means no spread to judge.
    """
    if len(rates) < 2:
        return 'only priced option', None
    ordered = sorted(rates)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2
              else (ordered[middle - 1] + ordered[middle]) / 2)
    median = round(median, 2)
    if not median:
        return 'only priced option', None
    share = rate / median
    if share <= 0.7:
        return 'best value', median
    if share <= 0.92:
        return 'good value', median
    return 'typical', median


def nutrition_snapshot():
    """USDA macros per ingredient, or None. No snapshot means no macro claims."""
    path = Path(os.environ.get('MEALS_NUTRITION') or catalogue_dir() / 'nutrition.json')
    return read_snapshot(path, 'nutrition') if path.is_file() else None


def ingredient_macros(quantity, unit, record):
    """Macros for this much of one ingredient, or None when they cannot be known.

    Grams and millilitres scale straight off the per-100 g figures. A counted
    ingredient needs the portion weight USDA publishes; without one there is no
    honest way to turn "one banana" into grams, so the caller names it unknown
    rather than inventing a weight.
    """
    if not record:
        return None
    per_100g = record.get('per_100g') or {}
    if unit in ('g', 'ml'):
        grams = quantity
    elif unit in ('un', 'sl'):
        serving = record.get('serving_g')
        if not serving:
            return None
        grams = quantity * serving
    elif unit == 'kg':
        grams = quantity * 1000
    else:
        return None
    share = grams / 100.0
    macros = {}
    for key in ('protein_g', 'carb_g', 'fat_g'):
        value = per_100g.get(key)
        if value is None:
            return None
        macros[key] = value * share
    # Fibre is the one field allowed to be missing on its own. USDA's Foundation
    # rows systematically omit it -- bread, pasta, milk, chicken breast, avocado
    # and yoghurt publish none -- and discarding the whole ingredient for that
    # left ten of the fifteen shipped recipes counting no protein either. The
    # protein, carbohydrate and fat are known, so they are reported, and the
    # caller names whose fibre it could not count.
    fibre = per_100g.get('fiber_g')
    macros['fiber_g'] = None if fibre is None else fibre * share
    return macros


def day_macros(meals, recipes, nutrition):
    """Sum a day's macros, naming every ingredient that could not contribute."""
    if not nutrition:
        unknown = sorted({part['item'] for meal in meals
                          for part in recipes[meal['recipe_id']]['ingredients']})
        return {'protein_g': None, 'carb_g': None, 'fat_g': None, 'fiber_g': None,
                'macros_unknown': unknown, 'macros_complete': False,
                'fiber_unknown': unknown}
    items = nutrition.get('items') or {}
    totals = {'protein_g': 0.0, 'carb_g': 0.0, 'fat_g': 0.0, 'fiber_g': 0.0}
    unknown, fibre_unknown = set(), set()
    for meal in meals:
        for part in recipes[meal['recipe_id']]['ingredients']:
            found = ingredient_macros(part['quantity'], part['unit'], items.get(part['item']))
            if found is None:
                unknown.add(part['item'])
                continue
            for key in ('protein_g', 'carb_g', 'fat_g'):
                totals[key] += found[key]
            # An ingredient can be counted for everything but fibre. Saying so
            # is the difference between a day that ate no fibre and a day whose
            # fibre nobody published.
            if found['fiber_g'] is None:
                fibre_unknown.add(part['item'])
            else:
                totals['fiber_g'] += found['fiber_g']
    return {**{key: round(value, 1) for key, value in totals.items()},
            'macros_unknown': sorted(unknown), 'macros_complete': not unknown,
            'fiber_unknown': sorted(fibre_unknown)}


def priced(quantity, unit, candidates):
    """Cheapest real price for this quantity: (cost, product, value, package_price).

    A candidate whose size did not parse has no unit price, so it can only offer
    its package price — the caller then keeps the catalogue estimate and says so,
    rather than inventing a per-gram figure.
    """
    usable = []
    for candidate in candidates or []:
        # A row the snapshot marked off-target is not this ingredient at any price.
        # Older snapshots carry no verdict, so absence means "not judged", not "no".
        if candidate.get('relevant') is False:
            continue
        scale = UNIT_SCALE.get((unit, candidate.get('unit')))
        if not candidate.get('unit_price') or scale is None:
            continue
        rate = candidate['unit_price']
        if candidate.get('promo') and candidate.get('price'):
            rate = rate * candidate['promo'] / candidate['price']
        usable.append((rate, scale, candidate))
    package = next((row.get('price') for row in candidates or []
                    if row.get('price') and row.get('relevant') is not False), None)
    if not usable:
        return None, None, None, package, None, None
    usable.sort(key=lambda found: found[0])
    rate, scale, chosen = usable[0]
    value, median = value_label(rate, [found[0] for found in usable])
    return (round(rate * quantity * scale, 2), chosen, value, chosen.get('price'),
            round(rate, 2), median)


def sex_of(value):
    lowered = text(value, 'sex', 20).strip().lower()
    if lowered in ('male', 'm', 'man'):
        return 'male'
    if lowered in ('female', 'f', 'woman'):
        return 'female'
    raise ValueError("sex must be male or female — the formula has no other coefficients")


def bmr(sex, weight_lb, height_in, age):
    """Resting energy, Mifflin-St Jeor."""
    kilos = positive(weight_lb, 'weight') * 0.45359237
    centimetres = positive(height_in, 'height') * 2.54
    base = 10 * kilos + 6.25 * centimetres - 5 * positive(age, 'age')
    return base + 5 if sex_of(sex) == 'male' else base - 161


def tdee(resting, activity):
    level = text(activity, 'activity', 40).strip().lower()
    if level not in ACTIVITY:
        raise ValueError('activity must be one of: ' + ', '.join(sorted(set(ACTIVITY))))
    return resting * ACTIVITY[level]


def target_calories(daily, goal, sex):
    """Target intake, and whether it had to be held at the floor."""
    aim = text(goal, 'goal', 40).strip().lower()
    if aim not in GOAL_SHIFT:
        raise ValueError('goal must be one of: ' + ', '.join(sorted(set(GOAL_SHIFT))))
    wanted = daily * (1 + GOAL_SHIFT[aim])
    floor = CALORIE_FLOOR[sex_of(sex)]
    if wanted < floor:
        return floor, True
    return int(round(wanted)), False


def macros(target, weight_lb, goal):
    """Grams of protein, carbohydrate and fat, plus fibre and water."""
    aim = text(goal, 'goal', 40).strip().lower()
    if aim not in PROTEIN_PER_LB:
        raise ValueError('goal must be one of: ' + ', '.join(sorted(set(PROTEIN_PER_LB))))
    protein_g = int(round(PROTEIN_PER_LB[aim] * positive(weight_lb, 'weight')))
    fat_g = int(round(FAT_SHARE * positive(target, 'target') / 9))
    remaining = target - protein_g * 4 - fat_g * 9
    if remaining <= 0:
        raise ValueError(f'{target} kcal cannot hold {protein_g} g protein and {fat_g} g fat; '
                         'raise the target or lower the protein goal')
    return {'protein_g': protein_g, 'fat_g': fat_g, 'carb_g': int(round(remaining / 4)),
            'fiber_g': max(25, int(round(14 * target / 1000))),
            'water_oz': int(round(weight_lb * 0.5))}


def targets(db, scope, args):
    """The day's numbers, computed from the profile's stats."""
    profile = read_profile(db, scope)
    missing = [name for name, key in (('age', 'age'), ('sex', 'sex'), ('height', 'height_in'),
                                      ('weight', 'weight_lb'), ('activity', 'activity'),
                                      ('goal', 'goal')) if profile.get(key) in (None, '')]
    if missing:
        raise ValueError('targets need ' + ', '.join(missing)
                         + ': profile set --age 30 --sex male --height-in 70 '
                           '--weight-lb 175 --activity moderate --goal maintenance')
    resting = bmr(profile['sex'], profile['weight_lb'], profile['height_in'], profile['age'])
    daily = tdee(resting, profile['activity'])
    target, floored = target_calories(daily, profile['goal'], profile['sex'])
    split = macros(target, profile['weight_lb'], profile['goal'])
    note = ('These are planning figures, not medical advice; a pre-existing condition, '
            'pregnancy or a history of disordered eating deserves a clinician.')
    if floored:
        note = (f"Held at the {CALORIE_FLOOR[sex_of(profile['sex'])]:,} kcal floor: the goal "
                'implied less, and going under that needs medical supervision. ') + note
    return {'bmr': int(round(resting)), 'tdee': int(round(daily)), 'calories': target,
            'floored': floored, 'goal': profile['goal'], 'activity': profile['activity'],
            'note': note, **split}


MACRO_KEYS = ('kcal', 'protein_g', 'carb_g', 'fat_g', 'fiber_g')
# Dialling codes, longest first so +351 is read before +35. Enough to tell whose
# stores a price belongs to; an unrecognised code stays unknown rather than
# guessing a country from a number.
DIALLING_CODES = (('351', 'PT'), ('55', 'BR'), ('49', 'DE'), ('44', 'GB'), ('39', 'IT'),
                  ('34', 'ES'), ('33', 'FR'), ('81', 'JP'), ('91', 'IN'), ('86', 'CN'),
                  ('61', 'AU'), ('52', 'MX'), ('54', 'AR'), ('1', 'US'))
COUNTRY_CURRENCY = {'US': 'USD', 'BR': 'BRL', 'GB': 'GBP', 'PT': 'EUR', 'DE': 'EUR',
                    'IT': 'EUR', 'ES': 'EUR', 'FR': 'EUR', 'JP': 'JPY', 'IN': 'INR',
                    'CN': 'CNY', 'AU': 'AUD', 'MX': 'MXN', 'AR': 'ARS'}


def country_from_phone(number):
    """Which country a number belongs to, or None. Needs the + : a bare string of
    digits could be anything, and a guessed country picks the wrong shops."""
    if not isinstance(number, str):
        return None
    digits = re.sub(r'[^0-9+]', '', number)
    if not digits.startswith('+'):
        return None
    digits = digits[1:]
    for code, country in DIALLING_CODES:
        if digits.startswith(code) and len(digits) > len(code) + 5:
            return country
    return None


def currency_for(country):
    """The currency of a country, or None. An unknown country invents nothing."""
    return COUNTRY_CURRENCY.get((country or '').strip().upper()) or None
# The order a shop is walked, not the order the alphabet falls in. "Other" is
# last and always exists: an ingredient nobody categorised still has to be bought.
SECTION_ORDER = ('Produce', 'Bakery', 'Meat & Fish', 'Dairy', 'Pantry', 'Frozen', 'Other')
# One per section, so a list scans at a glance on a phone. Kept here rather than
# left to the model: the same food should not be a different symbol each week.
SECTION_EMOJI = {'Produce': '🥬', 'Bakery': '🍞', 'Meat & Fish': '🥩', 'Dairy': '🥛',
                 'Pantry': '🫙', 'Frozen': '🧊', 'Other': '✨'}


def sectioned(items, categories):
    """Group a shopping list the way somebody walks a shop."""
    grouped = {}
    for held in items:
        name = categories.get(held['item']) or 'Other'
        if name not in SECTION_ORDER:
            name = 'Other'
        grouped.setdefault(name, []).append(held)
    return [{'name': name, 'emoji': SECTION_EMOJI[name], 'items': grouped[name],
             'cost': round(sum(held['cost'] for held in grouped[name]), 2)}
            for name in SECTION_ORDER if name in grouped]


def product_ingredient(prices, product_id):
    """Which catalogue ingredient a shelf product stands for, and its row."""
    for ingredient, rows in (prices.get('items') or {}).items():
        for row in rows or []:
            if row.get('product_id') == product_id:
                return ingredient, row
    raise ValueError(f'product {product_id} was not priced for this store')


def facts(db, scope, args):
    """What a product is for this person: a portion's macros, and the share of
    their day it uses.

    A photo on its own tells nobody anything. What belongs under it is this.
    Every nutrient the source does not carry is named in ``unknown`` rather than
    quietly left out: a caption missing fibre beside a target that names fibre
    would read as though the food had none.
    """
    profile = read_profile(db, scope)
    prices = prices_snapshot(profile['store_location_id'])
    if not prices:
        raise ValueError('no price snapshot for this conversation\'s store yet')
    ingredient, row = product_ingredient(prices, text(args.product_id, 'product id', 40))
    nutrition = (nutrition_snapshot() or {}).get('items', {}).get(ingredient)
    per_100g = (nutrition or {}).get('per_100g') or {}
    portion = (nutrition or {}).get('serving_g')
    found = {key: None for key in MACRO_KEYS}
    if portion:
        share = portion / 100.0
        for key in MACRO_KEYS:
            value = per_100g.get(key)
            if value is not None:
                found[key] = round(value * share, 1)
    unknown = [key for key in MACRO_KEYS if found[key] is None]

    target = None
    try:
        target = targets(db, scope, args)
    except ValueError:
        target = None
    portions = {'calories': 'kcal', 'protein_g': 'protein_g', 'carb_g': 'carb_g',
                'fat_g': 'fat_g', 'fiber_g': 'fiber_g'}
    share_of_day = None
    if target:
        share_of_day = {}
        for target_key, macro_key in portions.items():
            allowed, eaten = target.get(target_key), found.get(macro_key)
            share_of_day[f"{target_key.replace('_g', '')}_pct"] = (
                round(eaten / allowed * 100) if allowed and eaten else None)
    return {'ingredient': ingredient, 'product': row.get('description'),
            'brand': row.get('brand'), 'price': row.get('price'), 'size': row.get('size'),
            'unit_price': row.get('unit_price'), 'unit': row.get('unit'),
            'portion_g': portion, 'source': (nutrition or {}).get('description'),
            **found, 'unknown': unknown, 'share': share_of_day, 'target': target,
            'prices_captured': (prices.get('captured') or '')[:10]}


def override_days():
    """How long a confirmed price stays usable. A price that never expires is a
    hardcoded constant with better manners."""
    try:
        return max(int(os.environ.get('MEALS_OVERRIDE_DAYS', 14)), 0)
    except (TypeError, ValueError):
        return 14


def read_overrides(db, scope):
    rows = db.execute('SELECT item, price, unit, source, captured FROM overrides '
                      'WHERE scope=? ORDER BY item', (scope,)).fetchall()
    return [dict(row) for row in rows]


def override_command(db, scope, args):
    """Record, list or drop a price the person confirmed themselves."""
    if args.action == 'list':
        return {'overrides': read_overrides(db, scope), 'fresh_for_days': override_days()}
    item = text(args.item, 'item', 80)
    if args.action == 'clear':
        db.execute('DELETE FROM overrides WHERE scope=? AND item=?', (scope, item))
        db.commit()
        return {'cleared': item}
    captured = day_of(args.date, 'date').isoformat()
    saved = {'item': item, 'price': float(positive(args.price, 'price')),
             'unit': text(args.unit, 'unit', 20),
             'source': text(args.source, 'source', 200, optional=True), 'captured': captured}
    db.execute('''INSERT INTO overrides (scope, item, price, unit, source, captured)
                  VALUES (?,?,?,?,?,?) ON CONFLICT(scope, item) DO UPDATE SET
                  price=excluded.price, unit=excluded.unit, source=excluded.source,
                  captured=excluded.captured''',
               (scope, saved['item'], saved['price'], saved['unit'], saved['source'],
                saved['captured']))
    db.commit()
    return {'override': saved, 'fresh_for_days': override_days()}


def stores(db, scope, args):
    profile = read_profile(db, scope)
    if profile['lat'] is None or profile['lon'] is None:
        raise ValueError('set your location first: profile set --lat <n> --lon <n>')
    data = stores_snapshot()
    found = [dict(store, distance_km=round(haversine_km(
        profile['lat'], profile['lon'], store['lat'], store['lon']), 2))
        for store in data.get('stores', []) if store.get('lat') is not None]
    found.sort(key=lambda store: store['distance_km'])
    limit = int(positive(args.limit, 'limit')) if args.limit else 10
    return {'stores': found[:limit], 'source': data.get('source'),
            'licence': data.get('licence'), 'captured': data.get('captured')}


def read_profile(db, scope):
    row = db.execute('SELECT * FROM profiles WHERE scope=?', (scope,)).fetchone()
    if not row:
        raise ValueError('no profile for this conversation yet: '
                         'profile set --people <n> --calories <n>')
    saved = dict(row)
    saved.pop('scope')
    saved['diet'] = json.loads(saved['diet'])
    return saved


def set_profile(db, scope, args):
    current = db.execute('SELECT * FROM profiles WHERE scope=?', (scope,)).fetchone()
    saved = dict(current) if current else {
        'scope': scope, 'people': 1, 'calories': 2000, 'budget': None,
        'diet': '[]', 'lat': None, 'lon': None, 'address': None, 'currency': None,
        'store_location_id': None, 'age': None, 'sex': None, 'height_in': None,
        'weight_lb': None, 'activity': None, 'goal': None, 'phone': None,
        'country': None, 'price_country': None}
    if args.people is not None:
        saved['people'] = int(positive(args.people, 'people'))
    if args.calories is not None:
        saved['calories'] = int(positive(args.calories, 'calories'))
    if args.budget is not None:
        saved['budget'] = float(positive(args.budget, 'budget'))
    if args.diet is not None:
        saved['diet'] = json.dumps([text(part, 'diet', 40) for part in args.diet.split(',')
                                    if part.strip()])
    if args.lat is not None:
        saved['lat'] = float(args.lat)
    if args.lon is not None:
        saved['lon'] = float(args.lon)
    if args.address is not None:
        saved['address'] = text(args.address, 'address', 300)
    if args.store is not None:
        saved['store_location_id'] = text(args.store, 'store', 40)
    # Stats for the daily targets. Checked here so a wrong activity or goal is
    # refused while the person is still telling you about themselves.
    if args.age is not None:
        saved['age'] = int(positive(args.age, 'age'))
    if args.sex is not None:
        saved['sex'] = sex_of(args.sex)
    if args.height_in is not None:
        saved['height_in'] = float(positive(args.height_in, 'height'))
    if args.weight_lb is not None:
        saved['weight_lb'] = float(positive(args.weight_lb, 'weight'))
    if args.activity is not None:
        level = text(args.activity, 'activity', 40).strip().lower()
        if level not in ACTIVITY:
            raise ValueError('activity must be one of: ' + ', '.join(sorted(set(ACTIVITY))))
        saved['activity'] = level
    if args.goal is not None:
        aim = text(args.goal, 'goal', 40).strip().lower()
        if aim not in GOAL_SHIFT:
            raise ValueError('goal must be one of: ' + ', '.join(sorted(set(GOAL_SHIFT))))
        saved['goal'] = aim
    # The number says which country's shops are theirs, and that decides whose
    # prices mean anything. Their own currency follows from it.
    if getattr(args, 'phone', None) is not None:
        saved['phone'] = text(args.phone, 'phone', 40)
        found = country_from_phone(saved['phone'])
        if found:
            saved['country'] = found
            saved['currency'] = currency_for(found) or saved['currency']
    # Asked for, never assumed: somebody abroad may still want US prices, and
    # somebody who does not should not be shown them by default.
    if getattr(args, 'price_country', None) is not None:
        saved['price_country'] = text(args.price_country, 'price country', 8).strip().upper()
    saved['currency'] = text(args.currency, 'currency', 8, optional=True) or \
        saved['currency'] or catalogue()['currency']
    db.execute('''INSERT INTO profiles (scope, people, calories, budget, diet, lat, lon,
                  address, currency, store_location_id, age, sex, height_in, weight_lb,
                  activity, goal, phone, country, price_country)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(scope) DO UPDATE SET people=excluded.people,
                  calories=excluded.calories, budget=excluded.budget, diet=excluded.diet,
                  lat=excluded.lat, lon=excluded.lon, address=excluded.address,
                  currency=excluded.currency,
                  store_location_id=excluded.store_location_id, age=excluded.age,
                  sex=excluded.sex, height_in=excluded.height_in,
                  weight_lb=excluded.weight_lb, activity=excluded.activity,
                  goal=excluded.goal, phone=excluded.phone, country=excluded.country,
                  price_country=excluded.price_country''',
               (scope, saved['people'], saved['calories'], saved['budget'], saved['diet'],
                saved['lat'], saved['lon'], saved['address'], saved['currency'],
                saved['store_location_id'], saved['age'], saved['sex'], saved['height_in'],
                saved['weight_lb'], saved['activity'], saved['goal'], saved['phone'],
                saved['country'], saved['price_country']))
    db.commit()
    return {'profile': read_profile(db, scope)}


def build_plan(profile, data, start, days):
    servings = profile['people']
    diet = profile['diet']
    nutrition = nutrition_snapshot()
    recipes = {recipe['id']: recipe for recipe in data['recipes']}
    result = []
    for index in range(days):
        date = start + dt.timedelta(days=index)
        meals = []
        for slot in SLOTS:
            options = sorted((recipe for recipe in data['recipes']
                              if recipe['slot'] == slot and suits(recipe['tags'], diet)),
                             key=lambda recipe: recipe['id'])
            if not options:
                raise ValueError(f'no {slot} recipe in the catalogue matches '
                                 f'{", ".join(diet) or "this profile"}')
            # Rotate by day so a week varies, and by nothing else so replaying
            # the same week returns the same plan.
            recipe = options[index % len(options)]
            meals.append({'slot': slot, 'recipe_id': recipe['id'], 'title': recipe['title'],
                          'calories': recipe['calories'],
                          'cost': round(recipe['cost'] * servings, 2)})
        result.append({'date': date.isoformat(), 'meals': meals,
                       'calories': sum(meal['calories'] for meal in meals),
                       'cost': round(sum(meal['cost'] for meal in meals), 2),
                       **day_macros(meals, recipes, nutrition)})
    total = round(sum(day['cost'] for day in result), 2)
    budget = profile['budget']
    return {'start': start.isoformat(), 'days': result, 'total_cost': total,
            'budget': budget, 'over_budget': bool(budget is not None and total > budget),
            'currency': profile['currency'], 'people': servings,
            'calorie_target': profile['calories']}


def plan(db, scope, args):
    profile = read_profile(db, scope)
    start = day_of(args.start, 'start')
    if args.action == 'show':
        return {'plan': stored_plan(db, scope, start)}
    days = int(positive(args.days, 'days'))
    row = db.execute('SELECT payload, days FROM plans WHERE scope=? AND start=?',
                     (scope, start.isoformat())).fetchone()
    if row and row['days'] == days:
        return {'plan': json.loads(row['payload']), 'replayed': True}
    built = build_plan(profile, catalogue(), start, days)
    db.execute('''INSERT INTO plans (scope, start, days, payload) VALUES (?,?,?,?)
                  ON CONFLICT(scope, start) DO UPDATE SET days=excluded.days,
                  payload=excluded.payload''',
               (scope, start.isoformat(), days, json.dumps(built)))
    db.commit()
    return {'plan': built, 'replayed': False}


def stored_plan(db, scope, start):
    row = db.execute('SELECT payload FROM plans WHERE scope=? AND start=?',
                     (scope, start.isoformat())).fetchone()
    if not row:
        raise ValueError(f'no plan saved for the week of {start.isoformat()}')
    return json.loads(row['payload'])


def shopping(db, scope, args):
    profile = read_profile(db, scope)
    saved = stored_plan(db, scope, day_of(args.start, 'start'))
    recipes = {recipe['id']: recipe for recipe in catalogue()['recipes']}
    basket = {}
    for day in saved['days']:
        for meal in day['meals']:
            for part in recipes[meal['recipe_id']]['ingredients']:
                key = (part['item'], part['unit'])
                held = basket.setdefault(key, {'item': part['item'], 'quantity': 0,
                                               'unit': part['unit'], 'cost': 0.0})
                held['quantity'] += part['quantity'] * profile['people']
                held['cost'] += part['cost'] * profile['people']
    items = sorted(basket.values(), key=lambda held: held['item'])
    prices = prices_snapshot(profile['store_location_id'])
    # Whose shops are these? A price from the wrong country looks exactly like a
    # real one, which makes it worse than an admitted gap. The person can ask for
    # them anyway -- price_country records that they did.
    theirs = (profile.get('price_country') or profile.get('country') or '').strip().upper()
    snapshot_country = ((prices or {}).get('country') or 'US').strip().upper()
    suppressed = bool(prices and theirs and theirs != snapshot_country)
    because = (f'prices are {snapshot_country} store prices and this number is {theirs}; '
               f'ask for {snapshot_country} prices to see them anyway') if suppressed else None
    if suppressed:
        prices = None
    stamp = (prices or {}).get('captured', '')[:10]
    confirmed = {row['item']: row for row in read_overrides(db, scope)}
    cutoff = dt.date.today() - dt.timedelta(days=override_days())
    from_snapshot = 0
    stale, mismatched, by_hand = [], [], 0
    for held in items:
        held['quantity'] = round(held['quantity'], 2)
        estimate = round(held['cost'], 2)
        said = confirmed.get(held['item'])
        if said:
            # An explicit instruction outranks a lookup — the person was in the
            # shop. But only in the recipe's own unit, and only while fresh.
            if said['unit'] != held['unit']:
                mismatched.append(held['item'])
            elif day_of(said['captured'], 'captured') < cutoff:
                stale.append(held['item'])
            else:
                held.update(cost=round(said['price'] * held['quantity'], 2), estimate=estimate,
                            product=None, brand=None, value=None,
                            price_source=f"you confirmed {said['captured']}"
                                         + (f" ({said['source']})" if said['source'] else ''),
                            package_price=None, promo=None, image=None,
                            unit_price=said['price'], median_unit_price=None)
                by_hand += 1
                continue
        cost, product, value, package, rate, median = priced(
            held['quantity'], held['unit'], (prices or {}).get('items', {}).get(held['item']))
        if cost is None:
            held.update(cost=estimate, estimate=estimate, product=None, brand=None,
                        value=None, price_source='catalogue estimate',
                        package_price=package, promo=None, image=None,
                        unit_price=None, median_unit_price=None)
            continue
        held.update(cost=cost, estimate=estimate, product=product['description'],
                    brand=product.get('brand'), product_id=product.get('product_id'),
                    value=value,
                    price_source=f"kroger:{prices['location_id']} {stamp}",
                    package_price=product.get('price'), promo=product.get('promo'),
                    image=product.get('image'), unit_price=rate,
                    median_unit_price=median)
        from_snapshot += 1
    # The money these numbers are actually in, which is not always the reader's.
    # A suppressed list is USD-shaped catalogue estimates; labelling them BRL
    # because the reader is Brazilian is the same mislabelling, moved one field
    # over. their_currency carries what their money is.
    figures_in = ((prices or {}).get('currency') or catalogue().get('currency') or 'USD') \
        if prices else (catalogue().get('currency') or 'USD')
    return {'items': items, 'sections': sectioned(items, catalogue().get('categories') or {}),
            'total_cost': round(sum(held['cost'] for held in items), 2),
            'currency': figures_in, 'their_currency': profile['currency'],
            'start': saved['start'], 'people': profile['people'],
            'priced_from_snapshot': from_snapshot, 'confirmed_by_you': by_hand,
            'estimated': len(items) - from_snapshot - by_hand,
            'stale_overrides': stale, 'mismatched_overrides': mismatched,
            'prices_captured': stamp or None,
            'store_prices_suppressed': suppressed, 'suppressed_because': because,
            # The catalogue's figures are USD-shaped. Calling them reais because
            # the reader is Brazilian would be a different lie from the one this
            # suppression removes.
            'estimates_currency': catalogue().get('currency') or 'USD'}


def consumed_on(db, scope, date):
    row = db.execute('SELECT COALESCE(SUM(calories), 0) AS total FROM entries '
                     'WHERE scope=? AND date=?', (scope, date.isoformat())).fetchone()
    return int(row['total'])


def order(db, scope, args):
    if args.action == 'list':
        rows = db.execute('SELECT payload, status FROM orders WHERE scope=? '
                          'ORDER BY created DESC', (scope,)).fetchall()
        return {'orders': [dict(json.loads(row['payload']), status=row['status'])
                           for row in rows]}
    if args.action == 'confirm':
        return confirm_order(db, scope, text(args.id, 'order id', 64))
    profile = read_profile(db, scope)
    data = catalogue()
    date = day_of(args.date)
    slot = text(args.slot, 'slot', 20)
    remaining = max(profile['calories'] - consumed_on(db, scope, date), 0)
    craving = text(args.craving, 'craving', 60, optional=True)
    candidates = []
    for venue in data['venues']:
        away = distance_km(profile, venue)
        for item in venue['items']:
            if not suits(item.get('tags', []), profile['diet']):
                continue
            haystack = f"{venue['name']} {venue.get('cuisine', '')} {item['title']}".lower()
            if craving and craving.lower() not in haystack:
                continue
            candidates.append({'venue_id': venue['id'], 'venue': venue['name'],
                               'item': item['title'], 'price': item['price'],
                               'calories': item['calories'], 'distance_km': away,
                               'eta_min': venue.get('eta_min'), 'link': venue['link'],
                               'sample': bool(venue.get('sample')),
                               'fits_remaining': item['calories'] <= remaining})
    if not candidates:
        raise ValueError('no venue in the catalogue serves '
                         f'{craving or "that"} within {", ".join(profile["diet"]) or "this profile"}')
    if args.max_distance_km is not None:
        limit = positive(args.max_distance_km, 'max-distance-km')
        near = [option for option in candidates if option['distance_km'] <= limit]
        if not near:
            closest = min(option['distance_km'] for option in candidates)
            raise ValueError(f'nothing within the {limit} km distance limit; '
                             f'the closest match is {closest} km away')
        candidates = near
    candidates.sort(key=lambda option: (
        option['price'] + DISTANCE_WEIGHT * option['distance_km']
        + (0 if option['fits_remaining'] else OVERSHOOT_PENALTY), option['distance_km']))
    chosen = candidates[0]
    draft = dict(chosen, id=uuid.uuid4().hex[:12], date=date.isoformat(), slot=slot,
                 currency=profile['currency'], status='draft', remaining_before=remaining,
                 alternatives=candidates[1:4])
    db.execute('INSERT INTO orders (id, scope, date, status, created, payload) VALUES (?,?,?,?,?,?)',
               (draft['id'], scope, date.isoformat(), 'draft',
                dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                json.dumps(draft)))
    db.commit()
    return {'order': draft}


def confirm_order(db, scope, order_id):
    row = db.execute('SELECT payload, status FROM orders WHERE scope=? AND id=?',
                     (scope, order_id)).fetchone()
    if not row:
        raise ValueError(f'no order {order_id} in this conversation')
    saved = json.loads(row['payload'])
    if row['status'] == 'placed':
        return {'order': dict(saved, status='placed'), 'replayed': True}
    saved['status'] = 'placed'
    db.execute('UPDATE orders SET status=?, payload=? WHERE scope=? AND id=?',
               ('placed', json.dumps(saved), scope, order_id))
    db.execute('INSERT INTO entries (id, scope, date, title, calories, order_id) '
               'VALUES (?,?,?,?,?,?)',
               (uuid.uuid4().hex[:12], scope, saved['date'],
                f"{saved['item']} from {saved['venue']}", saved['calories'], order_id))
    db.commit()
    return {'order': saved, 'replayed': False}


def log(db, scope, args):
    read_profile(db, scope)
    date = day_of(args.date)
    entry = {'id': uuid.uuid4().hex[:12], 'title': text(args.title, 'title'),
             'calories': int(positive(args.calories, 'calories')), 'date': date.isoformat()}
    db.execute('INSERT INTO entries (id, scope, date, title, calories, order_id) '
               'VALUES (?,?,?,?,?,NULL)',
               (entry['id'], scope, entry['date'], entry['title'], entry['calories']))
    db.commit()
    return {'entry': entry}


def today(db, scope, args):
    profile = read_profile(db, scope)
    date = day_of(args.date)
    rows = db.execute('SELECT id, title, calories, order_id FROM entries '
                      'WHERE scope=? AND date=? ORDER BY rowid', (scope, date.isoformat())).fetchall()
    eaten = sum(int(row['calories']) for row in rows)
    return {'date': date.isoformat(), 'target': profile['calories'], 'consumed': eaten,
            'remaining': max(profile['calories'] - eaten, 0),
            'over_target': eaten > profile['calories'],
            'meals': [dict(row) for row in rows]}


def parser():
    parsed = argparse.ArgumentParser(description=__doc__)
    parsed.add_argument('--scope', required=True,
                        help='the trusted conversation this record belongs to')
    sub = parsed.add_subparsers(dest='command', required=True)

    profile = sub.add_parser('profile', help='who is eating, and where')
    actions = profile.add_subparsers(dest='action', required=True)
    setter = actions.add_parser('set')
    setter.add_argument('--people', type=int)
    setter.add_argument('--calories', type=int, help='daily target per person')
    setter.add_argument('--budget', type=float, help='for the planned period')
    setter.add_argument('--diet', help='comma separated, e.g. vegetarian,gluten-free')
    setter.add_argument('--lat', type=float)
    setter.add_argument('--lon', type=float)
    setter.add_argument('--address')
    setter.add_argument('--currency')
    setter.add_argument('--store', help='Kroger locationId whose price snapshot to use')
    setter.add_argument('--age', type=int)
    setter.add_argument('--sex', help='male or female; the formula has no other coefficients')
    setter.add_argument('--height-in', dest='height_in', type=float, help='total inches')
    setter.add_argument('--weight-lb', dest='weight_lb', type=float)
    setter.add_argument('--activity', help='sedentary, light, moderate, very active, athlete')
    setter.add_argument('--goal', help='fat loss, maintenance, muscle gain, performance')
    setter.add_argument('--phone', help="their own number, with its + : which country's "
                                        'shops are theirs')
    setter.add_argument('--price-country', dest='price_country',
                        help='whose prices they asked to see, when it is not their own')
    actions.add_parser('show')

    week = sub.add_parser('plan', help='plan meals for a run of days')
    week.add_argument('action', nargs='?', choices=['show'])
    week.add_argument('--days', type=int, default=7)
    week.add_argument('--start')

    basket = sub.add_parser('shopping', help='one list for a saved plan')
    basket.add_argument('--start')

    shops = sub.add_parser('stores', help='supermarkets near you, from the snapshot')
    shops.add_argument('--limit', type=int)

    said = sub.add_parser('override', help='a price the person confirmed themselves')
    said.add_argument('action', choices=['set', 'list', 'clear'])
    said.add_argument('--item')
    said.add_argument('--price', type=float)
    said.add_argument('--unit', help="the recipe's unit for this ingredient, such as un or g")
    said.add_argument('--source', help='where the price came from, in their words')
    said.add_argument('--date', help='when it was seen; defaults to today')

    delivery = sub.add_parser('order', help='draft an order, confirm one, or list them')
    delivery.add_argument('action', nargs='?', choices=['confirm', 'list'])
    delivery.add_argument('id', nargs='?')
    delivery.add_argument('--slot', default='dinner')
    delivery.add_argument('--date')
    delivery.add_argument('--craving')
    delivery.add_argument('--max-distance-km', dest='max_distance_km', type=float)

    eaten = sub.add_parser('log', help='record something eaten')
    eaten.add_argument('--title', required=True)
    eaten.add_argument('--calories', type=int, required=True)
    eaten.add_argument('--date')

    day = sub.add_parser('today', help='target, eaten and remaining')
    day.add_argument('--date')

    sub.add_parser('targets', help="the day's calories and macros, from the profile's stats")

    about = sub.add_parser('facts', help='what one product is: a portion, and the day it uses')
    about.add_argument('--product-id', dest='product_id', required=True,
                       help='from a shopping line, the same id the photo uses')
    return parsed


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    handlers = {'plan': plan, 'shopping': shopping, 'order': order, 'log': log, 'today': today,
                'stores': stores, 'override': override_command, 'targets': targets,
                'facts': facts}
    try:
        scope = text(args.scope, 'scope', 200)
        db = connect()
        if args.command == 'profile':
            result = set_profile(db, scope, args) if args.action == 'set' \
                else {'profile': read_profile(db, scope)}
        else:
            result = handlers[args.command](db, scope, args)
        json.dump(result, sys.stdout)
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        message = str(error) if isinstance(error, ValueError) else \
            f'{type(error).__name__}: {error}'
        json.dump({'error': message}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
