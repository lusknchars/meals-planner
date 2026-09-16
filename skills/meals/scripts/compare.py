#!/usr/bin/env python3
"""Compare nearby stores on a small basket, fast enough to answer in a reply.

Pricing every catalogue ingredient at every nearby store is 76 calls and about
two minutes per store. Eight staples is eight calls per store, so three stores
answer in seconds -- and a staple basket is what actually tells somebody where
to shop.

Lives beside meals.py because refresh.py is not in the agent's image; this and
photo.py are the only parts of the skill that touch the network while answering.
"""
import argparse
import base64
import gzip
import json
import math
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

KROGER_BASE = 'https://api.kroger.com/v1'
USER_AGENT = 'meals-planner/1.0 (store comparison)'
# Staples people actually buy weekly. Small on purpose: every extra item costs a
# call per store, and the point is an answer while somebody is still reading.
BASKET = ('milk', 'eggs', 'bread', 'rice', 'bananas', 'chicken breast', 'black beans', 'oats')
EARTH_KM = 6371.0


def _decoded(raw):
    """Kroger gzips bodies, error bodies included, so a refusal reads as binary
    noise unless it is decompressed -- the one message that has to be readable."""
    if raw[:2] == b'\x1f\x8b':
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return raw
    return raw


def http(method, url, headers=None, body=None, timeout=30):
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={'User-Agent': USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decoded(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decoded(error.read())


def _json(status, raw, what):
    if status // 100 != 2:
        raise ValueError(f'{what} answered {status}: {raw[:160].decode("utf-8", "replace")}')
    return json.loads(raw)


def token(fetch, client_id, client_secret):
    basic = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    body = urllib.parse.urlencode({'grant_type': 'client_credentials',
                                   'scope': 'product.compact'}).encode()
    status, raw = fetch('POST', f'{KROGER_BASE}/connect/oauth2/token',
                        headers={'Authorization': f'Basic {basic}',
                                 'Content-Type': 'application/x-www-form-urlencoded'},
                        body=body, timeout=30)
    found = _json(status, raw, 'Kroger token').get('access_token')
    if not found:
        raise ValueError('Kroger returned no access token')
    return found


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    half = (math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_KM * math.asin(math.sqrt(half))


def nearby_stores(fetch, bearer, zip_code, limit=3):
    url = (f'{KROGER_BASE}/locations?filter.zipCode.near={urllib.parse.quote(str(zip_code))}'
           f'&filter.limit={int(limit)}')
    status, raw = fetch('GET', url, headers={'Authorization': f'Bearer {bearer}'}, timeout=30)
    found = []
    for row in _json(status, raw, 'Kroger locations').get('data', []):
        address = row.get('address') or {}
        point = row.get('geolocation') or {}
        hours = row.get('hours') or {}
        found.append({'location_id': row.get('locationId'), 'name': row.get('name'),
                      'chain': row.get('chain'), 'address': address.get('addressLine1'),
                      'city': address.get('city'), 'zip': address.get('zipCode'),
                      'phone': row.get('phone'), 'lat': point.get('latitude'),
                      'lon': point.get('longitude'), 'timezone': hours.get('timezone'),
                      'hours': {day: hours[day] for day in
                                ('monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                                 'saturday', 'sunday') if isinstance(hours.get(day), dict)}})
    return found


def cheapest(fetch, bearer, location_id, term):
    """The lowest shelf price for one staple at one store, or None."""
    url = (f'{KROGER_BASE}/products?filter.term={urllib.parse.quote(term)}'
           f'&filter.locationId={urllib.parse.quote(str(location_id))}&filter.limit=5')
    status, raw = fetch('GET', url, headers={'Authorization': f'Bearer {bearer}'}, timeout=30)
    prices = []
    for product in _json(status, raw, 'Kroger products').get('data', []):
        item = (product.get('items') or [{}])[0]
        price = (item.get('price') or {})
        regular, promo = price.get('regular'), price.get('promo')
        paid = promo if promo else regular
        if paid:
            prices.append({'item': term, 'price': paid,
                           'description': product.get('description'),
                           'product_id': product.get('productId'), 'size': item.get('size')})
    if not prices:
        return None
    return min(prices, key=lambda row: row['price'])


def basket_total(priced):
    """What the basket costs here, or None when nothing could be priced."""
    if not priced:
        return None
    return round(sum(row['price'] for row in priced), 2)


def compare_stores(fetch, client_id, client_secret, zip_code, limit=3, lat=None, lon=None,
                   basket=BASKET):
    """Price one staple basket at each nearby store and rank them."""
    bearer = token(fetch, client_id, client_secret)
    stores = nearby_stores(fetch, bearer, zip_code, limit)
    for store in stores:
        priced = [row for row in
                  (cheapest(fetch, bearer, store['location_id'], term) for term in basket)
                  if row]
        store['items'] = priced
        store['priced'] = len(priced)
        store['basket_total'] = basket_total(priced)
        if lat is not None and lon is not None and store['lat'] and store['lon']:
            store['distance_km'] = round(
                haversine_km(lat, lon, store['lat'], store['lon']), 2)
    # A store that priced nothing cannot be cheapest: sort it last rather than
    # letting a missing total read as free.
    stores.sort(key=lambda store: (store['basket_total'] is None,
                                   store['basket_total'] or 0.0))
    return {'zip': str(zip_code), 'basket': list(basket), 'stores': stores}


def credentials(env_path=None):
    """Kroger keys from the environment, falling back to a private .env file."""
    found = {key: os.environ.get(key) for key in ('KROGER_CLIENT_ID', 'KROGER_CLIENT_SECRET')}
    if all(found.values()):
        return found['KROGER_CLIENT_ID'], found['KROGER_CLIENT_SECRET']
    path = Path(env_path or os.environ.get('MEALS_ENV')
                or Path('/var/lib/hermes/meals.env'))
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key.strip() in found and not found[key.strip()]:
                found[key.strip()] = value.strip().strip('"\'')
    if not all(found.values()):
        raise ValueError('set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in the environment '
                         f'or in {path}')
    return found['KROGER_CLIENT_ID'], found['KROGER_CLIENT_SECRET']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', dest='zip_code', required=True)
    parser.add_argument('--limit', type=int, default=3, help='how many stores to compare')
    parser.add_argument('--lat', type=float)
    parser.add_argument('--lon', type=float)
    parser.add_argument('--env')
    args = parser.parse_args(argv)
    try:
        client_id, client_secret = credentials(args.env)
        result = compare_stores(http, client_id, client_secret, args.zip_code,
                                limit=args.limit, lat=args.lat, lon=args.lon)
        json.dump(result, sys.stdout)
        return 0
    except (OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ValueError) else \
            f'{type(error).__name__}: {error}'
        json.dump({'error': message}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
