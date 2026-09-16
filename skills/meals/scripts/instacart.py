#!/usr/bin/env python3
"""Hand a saved week's shopping to Instacart as one link.

The agent holds no payment method and places nothing. Instacart's shopping list
page takes the list and returns a link; the person opens it, picks a store, and
checks out in Instacart with their own account. That keeps "prepare, never place"
true while still getting the food to somebody's door.

Like photo.py, this touches the network while answering, and only for this:
meals.py stays offline. Three rules carry it:

* Quantities go in units Instacart matches. A unit it does not know fails
  quietly on their side, so one with no Instacart word is named, not converted.
* No link for a number Instacart does not deliver to.
* The link that comes back must be Instacart's before it is passed on.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare  # noqa: E402  (http; its CLI only runs under __main__)
import meals  # noqa: E402

SERVERS = {'development': 'https://connect.dev.instacart.tools',
           'production': 'https://connect.instacart.com'}
ENDPOINT = '/idp/v1/products/products_link'
# The recipe unit on the left, Instacart's word on the right. Counted food is
# bought whole: half an avocado is still one avocado at the shop.
UNITS = {'g': 'gram', 'kg': 'kilogram', 'ml': 'milliliter', 'l': 'liter', 'un': 'each'}
UNIT_WORDS = {'sl': 'slices'}
# The catalogue names food the British way. Instacart searches American shelves.
SEARCH_NAMES = {'courgette': 'zucchini', 'yoghurt': 'yogurt', 'peppers': 'bell peppers'}
DELIVERS_TO = ('US', 'CA')
# A page lives this long at Instacart; the saved link is dropped a day earlier so
# nobody is handed one that expires while they shop.
EXPIRES_DAYS = 30
LINK_HOSTS = ('instacart.com', 'instacart.tools')
NOTE = ('Instacart prices its own shelves at the store the person picks. The '
        "list's figures are not what that cart will total, so give no total for it.")


def line_items(shopping):
    """Instacart line items for a shopping list, and the items sent without a measure."""
    lines, unmeasured = [], []
    for held in shopping['items']:
        name = SEARCH_NAMES.get(held['item'], held['item'])
        unit = UNITS.get(held['unit'])
        if unit is None:
            word = UNIT_WORDS.get(held['unit'], held['unit'])
            line = {'name': name, 'display_text': f"{name}, {held['quantity']:g} {word}"}
            unmeasured.append(held['item'])
        else:
            rounding = math.ceil if unit == 'each' else round
            line = {'name': name, 'quantity': max(int(rounding(held['quantity'])), 1),
                    'unit': unit}
        if held.get('preferred_brand'):
            line['filters'] = {'brand_filters': [held['preferred_brand']]}
        lines.append(line)
    return lines, unmeasured


def instacart_link(url):
    parts = urllib.parse.urlsplit(url if isinstance(url, str) else '')
    host = (parts.hostname or '').lower()
    return parts.scheme == 'https' and any(host == allowed or host.endswith('.' + allowed)
                                           for allowed in LINK_HOSTS)


def cache_file(home, environment, body):
    base = home or os.environ.get('HERMES_HOME')
    if not base:
        raise ValueError('HERMES_HOME must name this agent installation')
    folder = Path(base) / 'cache' / 'meals-instacart'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps([environment, body], sort_keys=True).encode())
    return folder / f'{digest.hexdigest()[:24]}.json'


def shopping_link(fetch, scope, start=None, key=None, environment=None, home=None):
    """One Instacart link for the saved plan starting on ``start``."""
    key = (key or '').strip()
    if not key:
        raise ValueError('INSTACART_API_KEY is not set: create a key at '
                         'dashboard.instacart.com and put it in .env')
    environment = environment or 'development'
    if environment not in SERVERS:
        raise ValueError('INSTACART_ENV must be development or production')
    db = meals.connect()
    profile = meals.read_profile(db, scope)
    theirs = (profile.get('price_country') or profile.get('country') or '').strip().upper()
    if theirs and theirs not in DELIVERS_TO:
        raise ValueError(f'Instacart delivers in the US and Canada, and this number is '
                         f'{theirs}, so there is no Instacart cart to build')
    shopping = meals.shopping(db, scope, argparse.Namespace(start=start))
    lines, unmeasured = line_items(shopping)
    body = {'title': f"Meals Planner shopping from {shopping['start']}",
            'link_type': 'shopping_list', 'expires_in': EXPIRES_DAYS, 'line_items': lines}
    described = {'title': body['title'], 'line_items': len(lines), 'unmeasured': unmeasured,
                 'environment': environment, 'note': NOTE}

    saved = cache_file(home, environment, body)
    today = dt.date.today()
    if saved.is_file():
        kept = json.loads(saved.read_text())
        if meals.day_of(kept['expires'], 'expires') > today:
            return dict(described, url=kept['url'], expires=kept['expires'], cached=True)

    status, raw = fetch('POST', SERVERS[environment] + ENDPOINT,
                        headers={'Authorization': f'Bearer {key}',
                                 'Content-Type': 'application/json',
                                 'Accept': 'application/json'},
                        body=json.dumps(body).encode(), timeout=30)
    if status in (401, 403):
        raise ValueError(f'Instacart refused the API key ({status}). A development key only '
                         'works with INSTACART_ENV=development, and a production key only '
                         'once Instacart has approved it')
    if status // 100 != 2:
        raise ValueError(f'Instacart answered {status}: '
                         f'{raw[:160].decode("utf-8", "replace")}')
    try:
        url = json.loads(raw).get('products_link_url')
    except (json.JSONDecodeError, AttributeError):
        url = None
    if not instacart_link(url):
        raise ValueError('Instacart did not return an Instacart link, so none is passed on')
    expires = (today + dt.timedelta(days=EXPIRES_DAYS - 1)).isoformat()
    saved.write_text(json.dumps({'url': url, 'expires': expires}))
    return dict(described, url=url, expires=expires, cached=False)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', required=True,
                        help='the trusted conversation this list belongs to')
    parser.add_argument('--start', help='the saved plan to buy; defaults to today')
    args = parser.parse_args(argv)
    try:
        found = shopping_link(compare.http, meals.text(args.scope, 'scope', 200), args.start,
                              key=os.environ.get('INSTACART_API_KEY'),
                              environment=os.environ.get('INSTACART_ENV'))
        json.dump(found, sys.stdout)
        return 0
    except (OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ValueError) else \
            f'{type(error).__name__}: {error}'
        json.dump({'error': message}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
