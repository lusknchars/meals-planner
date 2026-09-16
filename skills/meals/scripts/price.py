#!/usr/bin/env python3
"""What one food costs at one store, whatever the food is.

Asked "how much is milk", the agent had no tool for the question: compare prices
a fixed basket and facts needs a product id. So it reached for the web and quoted
figures it was told never to use. Kroger prices any term, so this exists.

Answers are cached per store, term and unit. Prices do not move hourly, and
asking twice should not spend a call or make anybody wait twice.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare  # noqa: E402  (token, http and credentials; its CLI only runs under __main__)
import foods  # noqa: E402

# Tried in order. Milk answers by the litre, rice by the kilo, eggs by the item;
# the first that finds a real product wins.
UNITS = ('l', 'kg', 'ct')
TTL_SECONDS = 6 * 60 * 60
CANDIDATES = 10


def cache_path(home, store, term, unit):
    # meals.py refuses this the same way. A bare KeyError reaches the model as
    # "KeyError: 'HERMES_HOME'", which says nothing about what to do next.
    base = home or os.environ.get('HERMES_HOME')
    if not base:
        raise ValueError('HERMES_HOME must name this agent installation')
    folder = Path(base) / 'cache' / 'meals-prices'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe = ''.join(letter if letter.isalnum() else '-' for letter in term.lower())[:40]
    return folder / f'{store}-{safe}-{unit or "any"}.json'


def read_cache(path, ttl_seconds):
    try:
        age = time.time() - path.stat().st_mtime
        if age > ttl_seconds:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def search(fetch, bearer, store, term):
    url = (f'{compare.KROGER_BASE}/products?filter.term={compare.urllib.parse.quote(term)}'
           f'&filter.locationId={compare.urllib.parse.quote(str(store))}'
           f'&filter.limit={CANDIDATES}')
    status, raw = fetch('GET', url, headers={'Authorization': f'Bearer {bearer}'}, timeout=30)
    return compare._json(status, raw, 'Kroger products').get('data', [])


def ranked(rows, term, unit):
    """Every product that really is this food, cheapest per unit first."""
    found = []
    for row in rows:
        best = foods.best_for({'term': term, 'quantity': 1, 'unit': unit}, [row])
        if best:
            found.append(best)
    found.sort(key=lambda row: row['unit_price'])
    return found


def price_item(fetch, client_id, client_secret, store, term, unit=None, home=None,
               ttl_seconds=None):
    """The cheapest real product for this food at this store, with alternatives."""
    term = (term or '').strip()
    if not term:
        raise ValueError('name a food to price')
    ttl_seconds = TTL_SECONDS if ttl_seconds is None else ttl_seconds
    path = cache_path(home, store, term, unit)
    cached = read_cache(path, ttl_seconds)
    if cached:
        return {**cached, 'cached': True}

    bearer = compare.token(fetch, client_id, client_secret)
    for attempt in ([unit] if unit else UNITS):
        rows = ranked(search(fetch, bearer, store, term), term, attempt)
        if rows:
            answer = {'term': term, 'store': str(store), 'unit': attempt,
                      'best': rows[0], 'alternatives': rows[1:4],
                      'captured': time.strftime('%Y-%m-%dT%H:%M:%S'), 'note': None}
            path.write_text(json.dumps(answer))
            return {**answer, 'cached': False}
    answer = {'term': term, 'store': str(store), 'unit': unit, 'best': None,
              'alternatives': [], 'captured': time.strftime('%Y-%m-%dT%H:%M:%S'),
              'note': f'no product at this store answers to {term}'}
    path.write_text(json.dumps(answer))
    return {**answer, 'cached': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--item', required=True, help='any food, not only catalogue ones')
    parser.add_argument('--store', required=True, help='the Kroger locationId to price at')
    parser.add_argument('--unit', choices=UNITS, help='skip the guess and compare in this unit')
    parser.add_argument('--env')
    args = parser.parse_args(argv)
    try:
        client_id, client_secret = compare.credentials(args.env)
        found = price_item(compare.http, client_id, client_secret, args.store, args.item,
                           unit=args.unit)
        json.dump(found, sys.stdout)
        return 0
    except (OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ValueError) else \
            f'{type(error).__name__}: {error}'
        json.dump({'error': message}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
