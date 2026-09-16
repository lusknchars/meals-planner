#!/usr/bin/env python3
"""Put a saved week's shopping in the person's own Kroger cart.

Kroger's cart API only adds: it cannot read the cart and cannot check out. The
person still picks the time, pickup or delivery, and pays in the Kroger or Ralphs
app, so "prepare, never place" stays true. The products that go in are the ones
the shopping list priced, preferred brands included.

The person allows it once by logging in to Kroger. This agent has no public
address for Kroger to send them back to, so they land on a static page that only
shows a code, and they send that code back here. The code is useless without this
installation's client secret and the PKCE verifier kept below.

What it refuses:

* a code from another conversation's login, or one finished after ten minutes;
* adding a list it already added, which would double the cart: only the
  difference goes in;
* a line with no store product, which is named instead of guessed at.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare  # noqa: E402  (http; its CLI only runs under __main__)
import meals  # noqa: E402

KROGER = 'https://api.kroger.com/v1'
DEFAULT_REDIRECT = 'https://lusknchars.github.io/meals-planner/kroger/'
SCOPE = 'cart.basic:write'
LOGIN_SECONDS = 10 * 60
LOGIN_CODE = re.compile(r'([0-9a-f]{16})\.([A-Za-z0-9._~+/=-]{1,3000})')
MODALITIES = ('PICKUP', 'DELIVERY')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS kroger_logins (
  scope TEXT PRIMARY KEY, state TEXT NOT NULL, verifier TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kroger_tokens (
  scope TEXT PRIMARY KEY, access TEXT NOT NULL, refresh TEXT, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kroger_added (
  scope TEXT NOT NULL, start TEXT NOT NULL, upc TEXT NOT NULL, quantity INTEGER NOT NULL,
  PRIMARY KEY (scope, start, upc));
'''
NOT_CONNECTED = ('this conversation has not connected a Kroger account yet: run connect and '
                 'send them the login link')
NOTE = ('These are in their Kroger cart, not bought. They choose the time and pay in the '
        'Kroger or Ralphs app, and nothing is ordered until they check out there. The '
        'prices were checked at this store, so they should pick it at checkout.')


def database():
    db = meals.connect()
    db.executescript(SCHEMA)
    return db


def clock(now):
    return int(time.time()) if now is None else int(now)


def login_url(scope, client_id, redirect_uri, now=None):
    """A Kroger login link for this conversation, valid for ten minutes."""
    state = secrets.token_hex(8)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    db = database()
    db.execute('''INSERT INTO kroger_logins (scope, state, verifier, created) VALUES (?,?,?,?)
                  ON CONFLICT(scope) DO UPDATE SET state=excluded.state,
                  verifier=excluded.verifier, created=excluded.created''',
               (scope, state, verifier, clock(now)))
    db.commit()
    query = urllib.parse.urlencode({
        'scope': SCOPE, 'response_type': 'code', 'client_id': client_id,
        'redirect_uri': redirect_uri, 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256'})
    return {'login_url': f'{KROGER}/connect/oauth2/authorize?{query}',
            'expires_minutes': LOGIN_SECONDS // 60}


def token_request(fetch, client_id, client_secret, fields):
    basic = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    status, raw = fetch('POST', f'{KROGER}/connect/oauth2/token',
                        headers={'Authorization': f'Basic {basic}',
                                 'Content-Type': 'application/x-www-form-urlencoded'},
                        body=urllib.parse.urlencode(fields).encode(), timeout=30)
    try:
        reply = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        reply = {}
    return status, reply


def save_tokens(db, scope, reply, now):
    if not reply.get('access_token'):
        raise ValueError('Kroger answered without an access token, so nothing was connected')
    expires = clock(now) + int(reply.get('expires_in') or 0) - 60
    db.execute('''INSERT INTO kroger_tokens (scope, access, refresh, expires) VALUES (?,?,?,?)
                  ON CONFLICT(scope) DO UPDATE SET access=excluded.access,
                  refresh=COALESCE(excluded.refresh, kroger_tokens.refresh),
                  expires=excluded.expires''',
               (scope, reply['access_token'], reply.get('refresh_token'), expires))


def link(fetch, scope, pasted, client_id, client_secret, redirect_uri, now=None):
    """Finish the login with the code the person sent back from the Kroger page."""
    text = meals.text(pasted, 'code', 4000)
    if text.lower().startswith('kroger:'):
        text = text[len('kroger:'):]
    # The page only ever shows URL-safe characters. Anything else did not come
    # from a login, and a quote or a space in a chat message is how a command
    # line gets something added to it.
    found = LOGIN_CODE.fullmatch(text)
    if not found:
        raise ValueError('that is not a Kroger login code: the page shows one starting '
                         'kroger: with only letters, digits and . _ - ~ + / =')
    state, code = found.group(1), found.group(2)
    db = database()
    pending = db.execute('SELECT state, verifier, created FROM kroger_logins WHERE scope=?',
                         (scope,)).fetchone()
    if not pending or not hmac.compare_digest(pending['state'], state):
        raise ValueError("that code is not from this conversation's latest Kroger login: "
                         'run connect again for a new link')
    if clock(now) - pending['created'] > LOGIN_SECONDS:
        db.execute('DELETE FROM kroger_logins WHERE scope=?', (scope,))
        db.commit()
        raise ValueError('that login is more than ten minutes old: run connect again '
                         'for a new link')
    # A code works once, so this login is spent whether Kroger takes it or not.
    db.execute('DELETE FROM kroger_logins WHERE scope=?', (scope,))
    db.commit()
    status, reply = token_request(fetch, client_id, client_secret, {
        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri,
        'code_verifier': pending['verifier']})
    if status // 100 != 2:
        reason = f": {reply['error']}" if isinstance(reply.get('error'), str) else ''
        raise ValueError(f'Kroger refused the login ({status}{reason}); a code works once and '
                         'only for a few minutes, so run connect again')
    save_tokens(db, scope, reply, now)
    db.commit()
    return {'connected': True}


def disconnect(scope):
    db = database()
    db.execute('DELETE FROM kroger_tokens WHERE scope=?', (scope,))
    db.execute('DELETE FROM kroger_logins WHERE scope=?', (scope,))
    db.commit()
    return {'disconnected': True}


def access_token(fetch, db, scope, client_id, client_secret, now):
    saved = db.execute('SELECT access, refresh, expires FROM kroger_tokens WHERE scope=?',
                       (scope,)).fetchone()
    if not saved:
        raise ValueError(NOT_CONNECTED)
    if saved['expires'] > clock(now):
        return saved['access']
    status, reply = token_request(fetch, client_id, client_secret, {
        'grant_type': 'refresh_token', 'refresh_token': saved['refresh'] or ''})
    if status // 100 != 2 or not reply.get('access_token'):
        db.execute('DELETE FROM kroger_tokens WHERE scope=?', (scope,))
        db.commit()
        raise ValueError(f'the Kroger login has expired ({status}): run connect again')
    save_tokens(db, scope, reply, now)
    db.commit()
    return reply['access_token']


def add(fetch, scope, start, modality, client_id, client_secret, now=None):
    """Add what the saved list priced to their Kroger cart, minus what is already there."""
    modality = (modality or '').strip().upper()
    if modality not in MODALITIES:
        raise ValueError('modality must be PICKUP or DELIVERY')
    db = database()
    profile = meals.read_profile(db, scope)
    theirs = (profile.get('price_country') or profile.get('country') or '').strip().upper()
    if theirs and theirs != 'US':
        raise ValueError(f'Kroger shops are in the US and this number is {theirs}, so there '
                         'is no Kroger cart to fill')
    if not db.execute('SELECT 1 FROM kroger_tokens WHERE scope=?', (scope,)).fetchone():
        raise ValueError(NOT_CONNECTED)
    shopping = meals.shopping(db, scope, argparse.Namespace(start=start))
    prices = meals.prices_snapshot(profile['store_location_id']) or {}
    already = {row['upc']: row['quantity'] for row in db.execute(
        'SELECT upc, quantity FROM kroger_added WHERE scope=? AND start=?',
        (scope, shopping['start']))}

    sending, added, in_cart, not_added, check = [], [], [], [], []
    for held in shopping['items']:
        upc = held.get('product_id')
        row = next((found for found in (prices.get('items') or {}).get(held['item']) or []
                    if found.get('product_id') == upc), None) if upc else None
        if not row:
            not_added.append(held['item'])
            continue
        # The shopping list already chose this product by what it costs to buy
        # the week's amount, and counted its packs; the cart takes that count.
        wanted = held.get('packages')
        if wanted is None or row.get('sold_by') == 'WEIGHT':
            check.append(held['item'])
        wanted = wanted or 1
        more = wanted - already.get(upc, 0)
        if more <= 0:
            in_cart.append(held['item'])
            continue
        sending.append({'upc': upc, 'quantity': more, 'modality': modality})
        added.append({'item': held['item'], 'product': row.get('description'), 'upc': upc,
                      'quantity': more, 'total_packages': wanted})

    if sending:
        token = access_token(fetch, db, scope, client_id, client_secret, now)
        status, raw = fetch('PUT', f'{KROGER}/cart/add',
                            headers={'Authorization': f'Bearer {token}',
                                     'Content-Type': 'application/json',
                                     'Accept': 'application/json'},
                            body=json.dumps({'items': sending}).encode(), timeout=30)
        if status == 401:
            db.execute('DELETE FROM kroger_tokens WHERE scope=?', (scope,))
            db.commit()
            raise ValueError('Kroger no longer accepts this login (401): run connect again')
        if status // 100 != 2:
            raise ValueError(f'Kroger did not add the items ({status}): '
                             f'{raw[:160].decode("utf-8", "replace")}')
        for line in added:
            db.execute('''INSERT INTO kroger_added (scope, start, upc, quantity) VALUES (?,?,?,?)
                          ON CONFLICT(scope, start, upc) DO UPDATE SET quantity=excluded.quantity''',
                       (scope, shopping['start'], line['upc'], line['total_packages']))
        db.commit()
    return {'added': added, 'already_in_cart': in_cart, 'not_added': not_added,
            'check_in_app': check, 'modality': modality,
            'store': (prices.get('store') or {}).get('name'), 'note': NOTE}


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', required=True,
                        help='the trusted conversation this cart belongs to')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('connect', help='a Kroger login link for this conversation')
    linking = sub.add_parser('link', help='finish the login with the code they sent back')
    linking.add_argument('--code', required=True)
    adding = sub.add_parser('add', help="put the saved list's products in their cart")
    adding.add_argument('--start', help='the saved plan to buy; defaults to today')
    adding.add_argument('--modality', required=True, help='PICKUP or DELIVERY')
    sub.add_parser('disconnect', help='forget this conversation\'s Kroger login')
    args = parser.parse_args(argv)
    try:
        scope = meals.text(args.scope, 'scope', 200)
        if args.command == 'disconnect':
            found = disconnect(scope)
        else:
            client_id = (os.environ.get('KROGER_CLIENT_ID') or '').strip()
            client_secret = (os.environ.get('KROGER_CLIENT_SECRET') or '').strip()
            if not client_id or not client_secret:
                raise ValueError('KROGER_CLIENT_ID and KROGER_CLIENT_SECRET must be set: they '
                                 'come from your app at developer.kroger.com')
            redirect = os.environ.get('KROGER_REDIRECT_URI') or DEFAULT_REDIRECT
            if args.command == 'connect':
                found = login_url(scope, client_id, redirect)
            elif args.command == 'link':
                found = link(compare.http, scope, args.code, client_id, client_secret, redirect)
            else:
                found = add(compare.http, scope, args.start, args.modality, client_id,
                            client_secret)
        json.dump(found, sys.stdout)
        return 0
    except (OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ValueError) else \
            f'{type(error).__name__}: {error}'
        json.dump({'error': message}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
