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

SCHEMA = '''
CREATE TABLE IF NOT EXISTS profiles (
  scope TEXT PRIMARY KEY, people INTEGER NOT NULL, calories INTEGER NOT NULL,
  budget REAL, diet TEXT NOT NULL, lat REAL, lon REAL, address TEXT,
  currency TEXT NOT NULL, store_location_id TEXT);
CREATE TABLE IF NOT EXISTS plans (
  scope TEXT NOT NULL, start TEXT NOT NULL, days INTEGER NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY (scope, start));
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, date TEXT NOT NULL,
  status TEXT NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, date TEXT NOT NULL,
  title TEXT NOT NULL, calories INTEGER NOT NULL, order_id TEXT);
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
    os.chmod(store, 0o600)
    return db


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
    package = next((row.get('price') for row in candidates or [] if row.get('price')), None)
    if not usable:
        return None, None, None, package
    usable.sort(key=lambda found: found[0])
    rate, scale, chosen = usable[0]
    value = 'only priced option' if len(usable) == 1 else 'best value'
    return round(rate * quantity * scale, 2), chosen, value, chosen.get('price')


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
        'store_location_id': None}
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
    saved['currency'] = text(args.currency, 'currency', 8, optional=True) or \
        saved['currency'] or catalogue()['currency']
    db.execute('''INSERT INTO profiles (scope, people, calories, budget, diet, lat, lon,
                  address, currency, store_location_id) VALUES (?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(scope) DO UPDATE SET people=excluded.people,
                  calories=excluded.calories, budget=excluded.budget, diet=excluded.diet,
                  lat=excluded.lat, lon=excluded.lon, address=excluded.address,
                  currency=excluded.currency,
                  store_location_id=excluded.store_location_id''',
               (scope, saved['people'], saved['calories'], saved['budget'], saved['diet'],
                saved['lat'], saved['lon'], saved['address'], saved['currency'],
                saved['store_location_id']))
    db.commit()
    return {'profile': read_profile(db, scope)}


def build_plan(profile, data, start, days):
    servings = profile['people']
    diet = profile['diet']
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
                       'cost': round(sum(meal['cost'] for meal in meals), 2)})
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
    stamp = (prices or {}).get('captured', '')[:10]
    from_snapshot = 0
    for held in items:
        held['quantity'] = round(held['quantity'], 2)
        estimate = round(held['cost'], 2)
        cost, product, value, package = priced(
            held['quantity'], held['unit'], (prices or {}).get('items', {}).get(held['item']))
        if cost is None:
            held.update(cost=estimate, estimate=estimate, product=None, value=None,
                        price_source='catalogue estimate', package_price=package,
                        promo=None, image=None)
            continue
        held.update(cost=cost, estimate=estimate, product=product['description'], value=value,
                    price_source=f"kroger:{prices['location_id']} {stamp}",
                    package_price=product.get('price'), promo=product.get('promo'),
                    image=product.get('image'))
        from_snapshot += 1
    return {'items': items, 'total_cost': round(sum(held['cost'] for held in items), 2),
            'currency': profile['currency'], 'start': saved['start'], 'people': profile['people'],
            'priced_from_snapshot': from_snapshot, 'estimated': len(items) - from_snapshot,
            'prices_captured': stamp or None}


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
    actions.add_parser('show')

    week = sub.add_parser('plan', help='plan meals for a run of days')
    week.add_argument('action', nargs='?', choices=['show'])
    week.add_argument('--days', type=int, default=7)
    week.add_argument('--start')

    basket = sub.add_parser('shopping', help='one list for a saved plan')
    basket.add_argument('--start')

    shops = sub.add_parser('stores', help='supermarkets near you, from the snapshot')
    shops.add_argument('--limit', type=int)

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
    return parsed


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    handlers = {'plan': plan, 'shopping': shopping, 'order': order, 'log': log, 'today': today,
                'stores': stores}
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
