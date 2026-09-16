#!/usr/bin/env python3
"""Build the snapshots the meals skill reads: nearby stores, and store prices.

Run out of band — at build time or on demand — never inside a chat turn. The
skill script stays offline and deterministic; this is the only part that talks
to the network, and it writes files the skill then reads.

Sources:
  * Stores: OpenStreetMap via Overpass. ODbL 1.0 — attribution required, and
    share-alike applies to a redistributed derived database.
  * Prices: the Kroger Products API (Ralphs and Food4Less cover California).
    Kroger's developer terms govern that data, so price snapshots stay out of
    Git and out of the published image.

Every network function takes a ``fetch`` callable so the whole module is
testable without a network: ``fetch(method, url, headers=, body=, timeout=)``
returns ``(status, bytes)``.
"""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse
import urllib.request

# Mirrors differ by query shape and by the hour: the radius query answers on the
# main endpoint while kumi times out, and a statewide area count is the reverse.
# So try them in order rather than pinning one and calling the outage ours.
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
OVERPASS_MIRRORS = (OVERPASS_URL, 'https://overpass.kumi.systems/api/interpreter',
                    'https://overpass.osm.ch/api/interpreter')
KROGER_BASE = 'https://api.kroger.com/v1'
USER_AGENT = 'meals-planner/1.0 (Hermes agent snapshot builder)'
SHOP_FILTER = '["shop"~"^(supermarket|greengrocer)$"]'
# California's bounding box, as a sanity check on coordinates given on the CLI.
CALIFORNIA = (32.53, -124.48, 42.01, -114.13)

WEIGHT_KG = {'oz': 0.0283495, 'lb': 0.453592, 'g': 0.001, 'kg': 1.0}
VOLUME_L = {'fl oz': 0.0295735, 'gal': 3.78541, 'qt': 0.946353,
            'pt': 0.473176, 'ml': 0.001, 'l': 1.0}
SIZE_RE = re.compile(r'^\s*(\d+(?:\.\d+)?|\d+\s*/\s*\d+)\s*'
                     r'(fl\s*oz|oz|lb|gal|qt|pt|ml|l|kg|g)\s*$', re.IGNORECASE)


def http(method, url, headers=None, body=None, timeout=60):
    """The real fetch. Returns (status, bytes); an HTTP error is a status, not a raise."""
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={'User-Agent': USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _json(status, raw, what):
    if status // 100 != 2:
        raise ValueError(f'{what} answered {status}: {raw[:200].decode("utf-8", "replace")}')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f'{what} returned unreadable JSON: {error}')


# Stores -------------------------------------------------------------------

def overpass_query(lat, lon, radius_km=5):
    """One radius query. ``out center`` so ways carry coordinates; without it a
    supermarket mapped as a building has none."""
    around = f'around:{int(radius_km * 1000)},{lat:g},{lon:g}'
    return (f'[out:json][timeout:60];'
            f'(node({around}){SHOP_FILTER};way({around}){SHOP_FILTER};);out center;')


def store_records(elements):
    """Normalise Overpass elements. An unnamed shop is dropped: it cannot be named
    to somebody as the place to go."""
    stores = []
    for element in elements:
        tags = element.get('tags') or {}
        name = tags.get('name')
        if not name:
            continue
        centre = element.get('center') or {}
        lat, lon = element.get('lat', centre.get('lat')), element.get('lon', centre.get('lon'))
        if lat is None or lon is None:
            continue
        number, street = tags.get('addr:housenumber'), tags.get('addr:street')
        stores.append({
            'id': f"osm:{element.get('type')}/{element.get('id')}",
            'name': name,
            'brand': tags.get('brand'),
            'shop': tags.get('shop'),
            'address': ' '.join(part for part in (number, street) if part) or None,
            'city': tags.get('addr:city'),
            'hours': tags.get('opening_hours'),
            'lat': lat,
            'lon': lon,
        })
    return stores


def haversine(lat1, lon1, lat2, lon2):
    """Kilometres between two points. Duplicated in the skill script on purpose:
    that script ships alone in the image and imports nothing from here."""
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    half = (math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(half))


def nearest(stores, lat, lon, limit=10):
    """Stores with their distance, closest first."""
    measured = [dict(store, distance_km=round(haversine(lat, lon, store['lat'], store['lon']), 2))
                for store in stores]
    measured.sort(key=lambda store: store['distance_km'])
    return measured[:limit]


def fetch_stores(fetch, lat, lon, radius_km=5):
    """Ask each mirror in turn; the first JSON answer wins. A mirror that returns
    an HTML error page is an outage there, not a reason to give up."""
    body = urllib.parse.urlencode({'data': overpass_query(lat, lon, radius_km)}).encode()
    problems = []
    for mirror in OVERPASS_MIRRORS:
        status, raw = fetch('POST', mirror,
                            headers={'User-Agent': USER_AGENT,
                                     'Content-Type': 'application/x-www-form-urlencoded'},
                            body=body, timeout=180)
        try:
            return store_records(_json(status, raw, f'Overpass ({mirror})').get('elements', []))
        except ValueError as error:
            problems.append(str(error).split(':')[0])
    raise ValueError('every Overpass mirror refused this query: ' + '; '.join(problems))


# Kroger -------------------------------------------------------------------

def kroger_token(fetch, client_id, client_secret):
    """Client-credentials token for public product data. The secret travels in the
    Basic header, never in the body."""
    basic = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    body = urllib.parse.urlencode({'grant_type': 'client_credentials',
                                   'scope': 'product.compact'}).encode()
    status, raw = fetch('POST', f'{KROGER_BASE}/connect/oauth2/token',
                        headers={'Authorization': f'Basic {basic}',
                                 'Content-Type': 'application/x-www-form-urlencoded'},
                        body=body, timeout=60)
    token = _json(status, raw, 'Kroger token').get('access_token')
    if not token:
        raise ValueError('Kroger token response carried no access_token')
    return token


def kroger_locations(fetch, token, zip_code, limit=5):
    url = (f'{KROGER_BASE}/locations?filter.zipCode.near={urllib.parse.quote(str(zip_code))}'
           f'&filter.limit={int(limit)}')
    status, raw = fetch('GET', url, headers={'Authorization': f'Bearer {token}'}, timeout=60)
    found = []
    for row in _json(status, raw, 'Kroger locations').get('data', []):
        address = row.get('address') or {}
        point = row.get('geolocation') or {}
        found.append({'location_id': row.get('locationId'), 'name': row.get('name'),
                      'chain': row.get('chain'),
                      'address': address.get('addressLine1'), 'city': address.get('city'),
                      'state': address.get('state'), 'zip': address.get('zipCode'),
                      'lat': point.get('latitude'), 'lon': point.get('longitude')})
    return found


def _front_image(product):
    """The medium front image, or None. A missing image is never a guessed URL."""
    for image in product.get('images') or []:
        if image.get('perspective') != 'front':
            continue
        for size in image.get('sizes') or []:
            if size.get('size') == 'medium' and size.get('url'):
                return size['url']
    return None


def kroger_products(fetch, token, location_id, term, limit=5):
    """Products with this store's prices. ``promo`` is 0 when nothing is on sale,
    which is not a price, so it becomes None."""
    url = (f'{KROGER_BASE}/products?filter.term={urllib.parse.quote(str(term))}'
           f'&filter.locationId={urllib.parse.quote(str(location_id))}&filter.limit={int(limit)}')
    status, raw = fetch('GET', url, headers={'Authorization': f'Bearer {token}'}, timeout=60)
    found = []
    for product in _json(status, raw, 'Kroger products').get('data', []):
        items = product.get('items') or [{}]
        first = items[0]
        price = (first.get('price') or {})
        regular, promo = price.get('regular'), price.get('promo')
        size = first.get('size')
        measured = unit_price(regular, size) if regular else None
        found.append({'product_id': product.get('productId'),
                      'description': product.get('description'),
                      'brand': product.get('brand'),
                      'size': size,
                      'sold_by': first.get('soldBy'),
                      'price': regular,
                      'promo': promo if promo else None,
                      'unit_price': measured[0] if measured else None,
                      'unit': measured[1] if measured else None,
                      'image': _front_image(product)})
    return found


def fetch_prices(fetch, client_id, client_secret, location_id, terms):
    """One product search per term, against one store."""
    token = kroger_token(fetch, client_id, client_secret)
    items = {}
    for term in terms:
        items[term] = kroger_products(fetch, token, location_id, term)
    return {'location_id': location_id, 'items': items}


def parse_size(value):
    """('32 oz') -> (32.0, 'oz'). Anything this cannot read returns None, so a
    caller shows the package price instead of inventing a per-gram figure."""
    if not isinstance(value, str):
        return None
    match = SIZE_RE.match(value)
    if not match:
        return None
    raw, unit = match.group(1), re.sub(r'\s+', ' ', match.group(2).lower())
    if '/' in raw:
        top, bottom = (part.strip() for part in raw.split('/'))
        try:
            amount = float(top) / float(bottom)
        except (ValueError, ZeroDivisionError):
            return None
    else:
        amount = float(raw)
    return amount, unit


def unit_price(price, size):
    """Price per kilo or per litre, or None when the size does not parse."""
    parsed = parse_size(size)
    if not parsed or not price:
        return None
    amount, unit = parsed
    if unit in WEIGHT_KG:
        return round(price / (amount * WEIGHT_KG[unit]), 2), 'kg'
    if unit in VOLUME_L:
        return round(price / (amount * VOLUME_L[unit]), 2), 'l'
    return None


# Snapshots ----------------------------------------------------------------

def write_snapshot(path, payload, source, licence, private=False):
    """Write one snapshot atomically, stamped with where it came from and when."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {'captured': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                'source': source, 'licence': licence, **payload}
    handle, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(handle, 'w') as file:
            json.dump(document, file, indent=1, sort_keys=True)
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def catalogue_terms(catalogue_path):
    """Every distinct ingredient in the recipe catalogue: one price lookup each."""
    data = json.loads(Path(catalogue_path).read_text())
    return sorted({part['item'] for recipe in data['recipes']
                   for part in recipe['ingredients']})


def credentials(env_path):
    """Kroger keys from the environment, falling back to a private .env file."""
    found = {key: os.environ.get(key) for key in ('KROGER_CLIENT_ID', 'KROGER_CLIENT_SECRET')}
    if all(found.values()):
        return found['KROGER_CLIENT_ID'], found['KROGER_CLIENT_SECRET']
    path = Path(env_path)
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            if key.strip() in found and not found[key.strip()]:
                found[key.strip()] = value.strip().strip('"\'')
    if not all(found.values()):
        raise ValueError('set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in the environment '
                         f'or in {path}')
    return found['KROGER_CLIENT_ID'], found['KROGER_CLIENT_SECRET']


def main(argv=None):
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    stores = sub.add_parser('stores', help='snapshot supermarkets near a point')
    stores.add_argument('--lat', type=float, required=True)
    stores.add_argument('--lon', type=float, required=True)
    stores.add_argument('--radius-km', type=float, default=5)
    stores.add_argument('--out', default=str(root / 'skills/meals/stores.json'))

    prices = sub.add_parser('prices', help='snapshot one Kroger store\'s prices')
    prices.add_argument('--zip', dest='zip_code', required=True)
    prices.add_argument('--location-id', help='skip the location lookup')
    prices.add_argument('--catalogue', default=str(root / 'skills/meals/catalogue.json'))
    prices.add_argument('--env', default=str(root / '.env'))
    prices.add_argument('--out-dir', default=str(root / 'skills/meals'))

    args = parser.parse_args(argv)
    try:
        if args.command == 'stores':
            south, west, north, east = CALIFORNIA
            if not (south <= args.lat <= north and west <= args.lon <= east):
                print('note: that point is outside California; snapshotting it anyway',
                      file=sys.stderr)
            found = fetch_stores(http, args.lat, args.lon, args.radius_km)
            near = nearest(found, args.lat, args.lon, limit=len(found))
            path = write_snapshot(args.out, {'stores': near, 'around': {'lat': args.lat,
                                  'lon': args.lon, 'radius_km': args.radius_km}},
                                  source='OpenStreetMap via Overpass', licence='ODbL 1.0')
            print(f'{len(near)} stores -> {path}')
            return 0

        client_id, client_secret = credentials(args.env)
        location_id = args.location_id
        if not location_id:
            token = kroger_token(http, client_id, client_secret)
            found = kroger_locations(http, token, args.zip_code)
            if not found:
                raise ValueError(f'no Kroger-family store near {args.zip_code}')
            location_id = found[0]['location_id']
            print(f"using {found[0]['name']} ({location_id})")
        terms = catalogue_terms(args.catalogue)
        snapshot = fetch_prices(http, client_id, client_secret, location_id, terms)
        path = write_snapshot(Path(args.out_dir) / f'prices.{location_id}.json', snapshot,
                              source='Kroger Products API',
                              licence='Kroger developer terms; not redistributed', private=True)
        priced = sum(1 for rows in snapshot['items'].values() if rows)
        print(f'{priced}/{len(terms)} ingredients priced -> {path}')
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
