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
import gzip
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
USDA_BASE = 'https://api.nal.usda.gov/fdc/v1'
# Nutrient name -> (unit that identifies it, key in the snapshot). The unit is
# part of the match because Energy is published in both kcal and kJ.
USDA_NUTRIENTS = {'Protein': ('g', 'protein_g'),
                  'Carbohydrate, by difference': ('g', 'carb_g'),
                  'Total lipid (fat)': ('g', 'fat_g'),
                  # SR Legacy calls it one thing, Foundation another. Matching
                  # only the first left oats, berries, chicken and avocado
                  # reporting no fibre at all -- against a target that names 38 g
                  # of it. The same shape as the Atwater energy names.
                  'Fiber, total dietary': ('g', 'fiber_g'),
                  'Total dietary fiber (AOAC 2011.25)': ('g', 'fiber_g')}
# Foundation rows never publish a bare "Energy" -- they give Atwater factors, and
# the specific factors are the better figure for a particular food. Lower is
# preferred. Matching only 'Energy' left every Foundation row with no calories.
USDA_ENERGY = {'Energy (Atwater Specific Factors)': 1,
               'Energy (Atwater General Factors)': 2, 'Energy': 3}
ENERGY_LABEL = {1: 'Atwater specific factors', 2: 'Atwater general factors',
                3: 'kcal as published'}
USER_AGENT = 'meals-planner/1.0 (Hermes agent snapshot builder)'
SHOP_FILTER = '["shop"~"^(supermarket|greengrocer)$"]'
# California's bounding box, as a sanity check on coordinates given on the CLI.
CALIFORNIA = (32.53, -124.48, 42.01, -114.13)

WEIGHT_KG = {'oz': 0.0283495, 'lb': 0.453592, 'g': 0.001, 'kg': 1.0}
VOLUME_L = {'fl oz': 0.0295735, 'gal': 3.78541, 'qt': 0.946353,
            'pt': 0.473176, 'ml': 0.001, 'l': 1.0}
SIZE_RE = re.compile(r'^\s*(\d+(?:\.\d+)?|\d+\s*/\s*\d+)\s*'
                     r'(fl\s*oz|oz|lb|gal|qt|pt|ml|l|kg|g|ct|count|each|ea)\s*$', re.IGNORECASE)
# Counted goods: eggs are "12 ct", avocados "1 each". Same idea, four spellings.
COUNT_UNITS = {'ct', 'count', 'each', 'ea'}
# The catalogue is written in British English; Kroger sells American groceries.
# The search term may differ from the catalogue name, but the snapshot key never
# does — the skill looks an ingredient up by the name its recipe uses.
TERM_ALIASES = {'courgette': 'zucchini', 'yoghurt': 'plain yogurt',
                'aubergine': 'eggplant', 'coriander': 'cilantro',
                'rocket': 'arugula', 'spring onion': 'green onion',
                'prawns': 'shrimp', 'mince': 'ground beef'}


def _decoded(raw):
    """Kroger gzips bodies even when identity encoding is requested, and a
    compressed 401 hides the one sentence that says why a key was refused."""
    if raw[:2] == b'\x1f\x8b':
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return raw
    return raw


def http(method, url, headers=None, body=None, timeout=60):
    """The real fetch. Returns (status, bytes); an HTTP error is a status, not a raise."""
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={'User-Agent': USER_AGENT,
                                              'Accept-Encoding': 'identity',
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decoded(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decoded(error.read())


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


def region_query(region):
    """Every supermarket in a state or country, by ISO 3166-2 code.

    A radius covers one city. "Is there anywhere to shop near me" should answer
    anywhere in the state, including the places no Kroger banner reaches.
    """
    code = text_code(region)
    return (f'[out:json][timeout:300];'
            f'area["ISO3166-2"="{code}"]->.here;'
            f'(node(area.here){SHOP_FILTER};way(area.here){SHOP_FILTER};);out center;')


def text_code(region):
    code = (region or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{2}-[A-Z0-9]{1,3}', code):
        raise ValueError('region must be an ISO 3166-2 code such as US-CA')
    return code


def fetch_region(fetch, region):
    """Every supermarket in a region, trying each mirror in turn."""
    body = urllib.parse.urlencode({'data': region_query(region)}).encode()
    problems = []
    for mirror in OVERPASS_MIRRORS:
        status, raw = fetch('POST', mirror,
                            headers={'User-Agent': USER_AGENT,
                                     'Content-Type': 'application/x-www-form-urlencoded'},
                            body=body, timeout=420)
        try:
            return store_records(_json(status, raw, f'Overpass ({mirror})').get('elements', []))
        except ValueError as error:
            problems.append(str(error).split(':')[0])
    raise ValueError('every Overpass mirror refused this region: ' + '; '.join(problems))


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
        hours = row.get('hours') or {}
        found.append({'location_id': row.get('locationId'), 'name': row.get('name'),
                      'chain': row.get('chain'),
                      'address': address.get('addressLine1'), 'city': address.get('city'),
                      'state': address.get('state'), 'zip': address.get('zipCode'),
                      'lat': point.get('latitude'), 'lon': point.get('longitude'),
                      # Somebody driving to collect a shopping list needs to know
                      # whether it is open and how to ring ahead. compare.py has
                      # carried these since it was written; this path had not.
                      'phone': row.get('phone'), 'timezone': hours.get('timezone'),
                      'hours': {day: hours[day] for day in
                                ('monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                                 'saturday', 'sunday') if isinstance(hours.get(day), dict)}})
    return found


# The shelf's name for a thing is not always the recipe's. Accepted as the same
# ingredient when judging whether a product is on target; unlike TERM_ALIASES,
# which decides what to SEARCH for, these decide what counts as a match.
SYNONYMS = {
    'chickpeas': ('chickpea', 'garbanzo'),
    'courgette': ('zucchini',), 'zucchini': ('courgette',),
    'aubergine': ('eggplant',), 'coriander': ('cilantro',),
    'rocket': ('arugula',), 'prawns': ('shrimp',),
    'spring onion': ('green onion', 'scallion'),
    'yoghurt': ('yogurt',), 'mince': ('ground beef',),
}


# Words by which a product says it is more than the ingredient: made up, mixed,
# flavoured, or sold as something else. Whole words only, and never when the
# ingredient's own name uses the word. "&" and "and" are not here: the shelf
# writes "S&W Garbanzo Beans" and "Peeled and Deveined Shrimp".
NOT_THE_INGREDIENT = frozenset((
    'overnight', 'smoothie', 'mix', 'with', 'seasoned', 'rotisserie', 'deli', 'lunchmeat',
    'roasted', 'stage', 'cereal', 'bar', 'bars', 'treats', 'flavored', 'flavoured',
    'cinnamon', 'vanilla', 'chocolate', 'honey'))


def _phrase_matches(phrase, text, compact):
    """Every word of the phrase present, trimmed for plurals; failing that, the
    phrase with separators removed — the shelf writes "Chick Peas" for chickpeas."""
    words = [word for word in re.split(r'[^a-z0-9]+', phrase.lower()) if word]
    if not words:
        return False
    for word in words:
        stem = word
        if len(stem) > 3 and stem.endswith('ies'):
            stem = stem[:-3] + 'y'
        elif len(stem) > 3 and stem.endswith('es'):
            stem = stem[:-2]
        elif len(stem) > 3 and stem.endswith('s'):
            stem = stem[:-1]
        if stem not in text and word not in text:
            break
    else:
        return True
    joined = re.sub(r'[^a-z0-9]', '', phrase.lower())
    if len(joined) > 4 and joined.endswith('s'):
        joined = joined[:-1]
    return bool(joined) and joined in compact


def relevant(ingredient, search, description):
    """Whether this product is the thing that was asked for.

    The recipe's name, the searched name and any known synonym each get a chance:
    "berries" matches "Strawberries", "plain yogurt" matches "Plain Low Fat
    Yogurt", "chickpeas" matches both "Garbanzo Beans" and "Chick Peas", and
    "banana" matches no plantain.

    This is a name check, not a category check. It keeps out products that never
    claim to be the ingredient, and products whose name says they are more than
    it: overnight oats, a couscous mix, a deli turkey. A cereal bar that does not
    call itself one still gets through.
    """
    text = (description or '').lower()
    compact = re.sub(r'[^a-z0-9]', '', text)
    # An alias REPLACES the catalogue name rather than sitting beside it: the
    # alias is the more specific instruction. Judging "yoghurt" as well as
    # "plain yogurt" would let a blueberry dessert cup through on the bare word.
    phrase = (search or ingredient or '').strip().lower()
    if not phrase:
        return False
    own = set(re.findall(r'[a-z]+', f'{phrase} {(ingredient or "").lower()}'))
    if (set(re.findall(r'[a-z]+', text)) & NOT_THE_INGREDIENT) - own:
        return False
    names = [phrase]
    # A synonym renames one word and keeps the rest of the phrase qualified.
    for word in phrase.split():
        for other in SYNONYMS.get(word, ()):
            names.append(phrase.replace(word, other))
    names.extend(SYNONYMS.get(phrase, ()))
    return any(_phrase_matches(name, text, compact) for name in names if name)


def _front_image(product):
    """The medium front image, or None. A missing image is never a guessed URL."""
    for image in product.get('images') or []:
        if image.get('perspective') != 'front':
            continue
        for size in image.get('sizes') or []:
            if size.get('size') == 'medium' and size.get('url'):
                return size['url']
    return None


def kroger_products(fetch, token, location_id, term, limit=5, ingredient=None):
    """Products with this store's prices. ``promo`` is 0 when nothing is on sale,
    which is not a price, so it becomes None. Each row carries a verdict on whether
    it is actually the ingredient, judged here where the search term is known."""
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
                      'relevant': relevant(ingredient or term, term,
                                           product.get('description')),
                      'image': _front_image(product)})
    return found


def search_term(ingredient):
    """What to ask Kroger for. The catalogue's own name stays the key."""
    return TERM_ALIASES.get(ingredient.strip().lower(), ingredient)


def fetch_prices(fetch, client_id, client_secret, location_id, terms):
    """One product search per term, against one store."""
    token = kroger_token(fetch, client_id, client_secret)
    items = {}
    for term in terms:
        items[term] = kroger_products(fetch, token, location_id, search_term(term),
                                      ingredient=term)
    return {'location_id': location_id, 'items': items}


# Forms that are not the ingredient, whatever the name says. "Oil, oat" and
# "Bacon, meatless" both name their ingredient and both would poison a diet plan.
USDA_REJECT = ('oil', 'flour', 'powder', 'dehydrated', 'dried', 'meatless', 'substitute',
               'juice', 'babyfood', 'baby food', 'snacks', 'chips', 'bagels', 'bread',
               'muffins', 'crackers', 'cereals', 'mayonnaise', 'sticks', 'grease',
               'vegetarian', 'imitation', 'sauce', 'soup', 'candy', 'beverage',
               'pudding', 'puddings', 'overripe')
# Words that describe a cut or state rather than the food. Stripped before
# requiring the rest: "salmon fillet" must accept "Fish, salmon, sockeye, raw".
USDA_GENERIC = ('fillet', 'fillets', 'fresh', 'whole', 'ground', 'chopped', 'sliced',
                'breast', 'raw', 'large', 'medium', 'small')
# Whole, plain forms to prefer once the rejects are gone.
USDA_PREFER = ('raw', 'unprepared', 'whole', 'plain', 'uncooked')
# Ingredients no ranking rule rescues, because the right row is not in a bare
# search at all. These name the query that finds it.
USDA_QUERIES = {
    'oats': 'oats whole grain rolled',
    'bacon': 'pork cured bacon unprepared',
    'salmon fillet': 'fish salmon atlantic raw',
    'tofu': 'tofu raw firm prepared with calcium sulfate',
    'chickpeas': 'chickpeas garbanzo beans mature seeds cooked',
    'yoghurt': 'yogurt plain whole milk',
    'tapioca flour': 'tapioca pearl dry',
    'eggs': 'eggs grade a large egg whole',
    # A bare search returned "Cloudberries, raw (Alaska Native)": a subsistence
    # food with no portion weight, standing in for the strawberries a plan prices.
    'berries': 'strawberries raw',
    # Scoring cannot separate buttermilk, sheep, buffalo and chocolate milk from
    # the plain stuff, and a bare search had settled on ricotta cheese.
    'milk': 'milk whole 3.25% milkfat',
}
# Ingredients whose own name carries a word USDA never uses for the food. The
# reject list already drops such a word (it IS the ingredient, so it cannot
# disqualify it) but the word stayed REQUIRED, and "Tapioca, pearl, dry" -- the
# first candidate USDA offers -- was refused for lacking "flour". The ingredient
# went unmatched entirely, so a tapioca breakfast counted no macros at all.
# Narrow on purpose: "almond flour" really must say flour.
USDA_REQUIRED = {'tapioca flour': ('tapioca',)}
# Ingredients whose reference row is named outright, because search ranking is
# not good enough to pick a food unattended and "38/38 matched" hid a catalogue
# full of the wrong ones: almond butter for butter, wild rice for rice, green
# snap beans for the beans in rice and beans, and a frozen cheese turnover for
# tomato sauce. Every id below was read before it was written down.
#
# Mostly SR Legacy: it publishes fibre and calories where Foundation omits both,
# which is also how bread, pasta and olive oil got their calories back.
USDA_FDC = {
    'bread': 174924,           # Bread, white, commercially prepared
    'butter': 173410,          # Butter, salted          (was: Almond butter, creamy)
    'carrot': 170393,          # Carrots, raw            (was: Carrots, baby, raw)
    'cheese': 328637,          # Cheese, cheddar         (was: Cheese, caraway)
    'chicken breast': 171077,  # breast, skinless, boneless -- a recipe never means the skin
    'mushrooms': 169251,       # Mushrooms, white, raw   (was: enoki)
    'olive oil': 171413,       # Oil, olive, salad or cooking (was: extra light, no calories)
    'onion': 170000,           # Onions, raw             (was: Onions, red, raw)
    'pasta': 169736,           # Pasta, dry, enriched
    'potatoes': 170026,        # Potatoes, flesh and skin, raw (was: the skin alone)
    'rice': 168877,            # Rice, white, long-grain, raw (was: Wild rice)
    'soy sauce': 174277,       # soy and wheat (shoyu)   (was: tamari)
    'tomato': 170457,          # Tomatoes, red, ripe, raw, year round average
    'tomato sauce': 170054,    # Tomato products, canned, sauce (was: a frozen turnover)
    'black beans': 173734,     # Beans, black, mature seeds, raw
    # Added with the catalogue's second twenty-five recipes. Each read before
    # it was written: the bare search offered ground TURKEY for ground beef,
    # cashews for almonds, and an apple row with no calories at all.
    'almonds': 170158,         # dry roasted, unsalted -- the raw row has no calories
    'apple': 167793,           # Apples, raw, fuji, with skin
    'cauliflower': 169986,
    'cottage cheese': 172182,
    'couscous': 169699,
    'feta': 173420,
    'garlic': 169230,
    'green beans': 169961,     # snap beans, the row that once stood in for dry beans
    'ground beef': 174030,     # 90% lean, RAW   (the search offered Turkey, ground)
    'mozzarella': 170845,
    'orange': 169097,
    'peas': 170419,
    'shrimp': 175179,
    'sweet corn': 169998,      # two words: "sweetcorn" appears in no USDA description
    'tuna': 171986,            # light, canned in water
    'turkey breast': 171098,   # serving_g is a whole 1.8 kg breast -- grams only, never `un`
}


def usda_query(ingredient):
    """What to ask USDA for. Falls back to the ordinary search term."""
    return USDA_QUERIES.get((ingredient or '').strip().lower(), search_term(ingredient))


def best_food(candidates, ingredient, search):
    """The row that is actually this ingredient, or None.

    Scores what survives: a rejected form never qualifies, the name still has to
    appear, and a plain or raw entry beats a processed one. Returning None is a
    real answer — an ingredient with no acceptable row is recorded unmatched,
    because approximately-right macros are worse than admitted unknowns.
    """
    # Judge against the INGREDIENT, not the query. A curated query like "oats
    # whole grain rolled" would demand every one of those words, and "salmon
    # fillet" would reject "Fish, salmon, chinook, raw" for lacking "fillet" --
    # so fall back to the ingredient's head word when the full phrase finds
    # nothing. The search string still decides what USDA was asked for.
    words = [word for word in re.split(r'[^a-z0-9]+', (ingredient or '').lower()) if word]
    override = USDA_REQUIRED.get((ingredient or '').strip().lower())
    required = list(override) if override else (
        [word for word in words if word not in USDA_GENERIC] or words)
    # A form word that IS the ingredient cannot disqualify it: rejecting "oil"
    # threw away olive oil, "bread" threw away bread, "sauce" threw away soy sauce.
    # Whole words only. "oil" is a reject word and "boiled" contains it, which
    # threw away every cooked food USDA publishes -- chickpeas, asparagus, beets.
    rejects = [word for word in USDA_REJECT if word not in ' '.join(words)]
    reject_re = re.compile(r'\b(' + '|'.join(re.escape(word) for word in rejects) + r')\b'
                           ) if rejects else None
    asked = [word for word in re.split(r'[^a-z0-9]+', (search or '').lower()) if word]
    scored = []
    for row in candidates or []:
        description = (row.get('description') or '')
        lowered = description.lower()
        if reject_re and reject_re.search(lowered):
            continue
        # Every distinctive word, not just one of them: matching "black" alone
        # accepted a plum for black beans.
        if not all(relevant(word, word, description) for word in required):
            continue
        score = 0
        if any(word in lowered for word in USDA_PREFER):
            score += 3
        if (row.get('dataType') or '') == 'Foundation':
            score += 1
        # The curated query describes the row we want; each of its words that
        # lands is evidence. "Pork, cured, bacon" beats "Bacon, turkey".
        score += sum(1 for word in asked if word in lowered)
        # A short description is usually the plain food: "Bananas, raw" over
        # "Babyfood, banana no tapioca, strained".
        score -= len(description) / 200.0
        scored.append((score, row))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def usda_key(env_path=None):
    """A free key from fdc.nal.usda.gov; DEMO_KEY is rate-limited and for
    development only.

    The environment wins, then .env, then the demo key. Reading the file matters:
    the README tells people to put the key there, and for one release this
    function ignored it and spent DEMO_KEY's quota instead.
    """
    found = os.environ.get('USDA_API_KEY')
    if found:
        return found
    path = Path(env_path or Path(__file__).resolve().parent / '.env')
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key.strip() == 'USDA_API_KEY':
                cleaned = value.strip().strip('"\'')
                if cleaned:
                    return cleaned
    return 'DEMO_KEY'


def usda_search(fetch, api_key, term, ingredient=None):
    """The best reference match for an ingredient, or None.

    Twenty-five candidates, not one: USDA orders results alphabetically rather
    than by relevance, so the right row is often fifth and the first is an oil.
    """
    url = (f'{USDA_BASE}/foods/search?api_key={urllib.parse.quote(str(api_key))}&pageSize=25'
           f'&dataType={urllib.parse.quote("Foundation,SR Legacy")}'
           f'&query={urllib.parse.quote(str(term))}')
    status, raw = fetch('GET', url, headers={'User-Agent': USER_AGENT}, timeout=60)
    foods = _json(status, raw, 'USDA search').get('foods') or []
    chosen = best_food(foods, ingredient or term, term)
    if not chosen:
        return None
    return {'fdc_id': chosen.get('fdcId'), 'description': chosen.get('description')}


def usda_food(fetch, api_key, fdc_id):
    url = f'{USDA_BASE}/food/{int(fdc_id)}?api_key={urllib.parse.quote(str(api_key))}'
    status, raw = fetch('GET', url, headers={'User-Agent': USER_AGENT}, timeout=60)
    return _json(status, raw, 'USDA food')


def nutrition_record(detail):
    """Macros per 100 g, plus published portion weights.

    A nutrient the source does not carry stays None: unknown fibre is not zero
    fibre. Energy is published twice, in kcal and kJ, so the unit decides which
    row is the calorie figure -- matching on the name alone reads a banana as
    371 calories.
    """
    per_100g = {key: None for key in ('kcal', 'protein_g', 'carb_g', 'fat_g', 'fiber_g')}
    per_100g['energy_source'] = None
    kilojoules, energy = None, None
    for row in detail.get('foodNutrients') or []:
        nutrient = row.get('nutrient') or {}
        name, unit_name = nutrient.get('name'), (nutrient.get('unitName') or '').lower()
        if name in USDA_ENERGY and unit_name == 'kcal' and row.get('amount') is not None:
            rank = USDA_ENERGY[name]
            if energy is None or rank < energy[0]:
                energy = (rank, row.get('amount'))
        if name == 'Energy' and unit_name == 'kj' and kilojoules is None:
            kilojoules = row.get('amount')
        wanted = USDA_NUTRIENTS.get(name)
        if not wanted:
            continue
        unit, key = wanted
        if unit_name != unit.lower():
            continue
        if per_100g[key] is None:
            per_100g[key] = row.get('amount')
    if energy is not None:
        per_100g['kcal'] = round(energy[1], 1)
        per_100g['energy_source'] = ENERGY_LABEL[energy[0]]
    # Some rows carry energy only in kilojoules; 4.184 kJ to the calorie.
    if per_100g['kcal'] is None and kilojoules is not None:
        per_100g['kcal'] = round(kilojoules / 4.184, 1)
        per_100g['energy_source'] = 'converted from kJ'
    portions = [{'label': portion.get('modifier') or portion.get('portionDescription'),
                 'grams': portion.get('gramWeight')}
                for portion in (detail.get('foodPortions') or []) if portion.get('gramWeight')]
    serving = next((portion['grams'] for portion in portions
                    if (portion['label'] or '').strip().lower() == 'nlea serving'),
                   portions[0]['grams'] if portions else None)
    return {'fdc_id': detail.get('fdcId'), 'description': detail.get('description'),
            'per_100g': per_100g, 'portions': portions, 'serving_g': serving}


def fetch_nutrition(fetch, api_key, terms):
    """One search and one detail call per ingredient, keyed by the catalogue name.

    A refusal part-way through stops the run and keeps what was already fetched:
    DEMO_KEY allows about thirty calls an hour and a full catalogue needs more,
    so throwing away thirty good lookups to report one failure helps nobody. The
    ingredients never reached are named in ``pending``.
    """
    items, unmatched, pending, stopped = {}, [], [], None
    remaining = list(terms)
    while remaining:
        term = remaining.pop(0)
        try:
            # A pinned row skips the search entirely: the question "which USDA
            # food is this" was answered once, by reading it, and a ranking
            # cannot un-answer it next time the index shifts.
            pinned = USDA_FDC.get(term)
            found = ({'fdc_id': pinned, 'description': None} if pinned
                     else usda_search(fetch, api_key, usda_query(term), ingredient=term))
            if not found:
                items[term] = None
                unmatched.append(term)
                continue
            # Deliberately no fibre fallback. Foundation rows omit fibre for
            # bread, pasta, milk, chicken and avocado, and borrowing it from an
            # SR Legacy sibling was tried three ways: every one produced a
            # plausible number from the wrong food -- "Bread, white wheat" at
            # 9.2 g for white bread, breaded chicken tenders' 1.1 g for a food
            # with no fibre at all, a boxed beef pasta mix for dry spaghetti.
            # The ingredient keeps its own row's unknown instead, and
            # ``ingredient_macros`` reports the protein and fat it does know.
            items[term] = nutrition_record(usda_food(fetch, api_key, found['fdc_id']))
        except (ValueError, OSError) as refusal:
            # OSError covers URLError: an SSL handshake timeout mid-run threw away
            # every lookup already made, which is what this guard exists to stop.
            stopped = f'{term}: {refusal}'
            pending = [term] + remaining
            break
    return {'items': items, 'unmatched': unmatched, 'pending': pending, 'stopped': stopped}


def parse_size(value):
    """('32 oz') -> (32.0, 'oz'). Anything this cannot read returns None, so a
    caller shows the package price instead of inventing a per-gram figure."""
    if not isinstance(value, str):
        return None
    match = SIZE_RE.match(value)
    if not match:
        return None
    raw, unit = match.group(1), re.sub(r'\s+', ' ', match.group(2).lower())
    if unit in COUNT_UNITS:
        unit = 'ct'
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
    if unit == 'ct':
        return round(price / amount, 2), 'ct'
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

    stores = sub.add_parser('stores', help='snapshot supermarkets near a point, or a whole region')
    stores.add_argument('--region', help='ISO 3166-2 code, such as US-CA, for a whole state')
    stores.add_argument('--lat', type=float)
    stores.add_argument('--lon', type=float)
    stores.add_argument('--radius-km', type=float, default=5)
    stores.add_argument('--out', default=str(root / 'skills/meals/stores.json'))

    nutrients = sub.add_parser('nutrition', help='snapshot macros for every catalogue ingredient')
    nutrients.add_argument('--catalogue', default=str(root / 'skills/meals/catalogue.json'))
    nutrients.add_argument('--out', default=str(root / 'skills/meals/nutrition.json'))
    nutrients.add_argument('--api-key', dest='api_key',
                           help='a free FoodData Central key; DEMO_KEY is development only')
    nutrients.add_argument('--env', default=str(root / '.env'),
                           help='file holding USDA_API_KEY, as the Kroger path does')

    prices = sub.add_parser('prices', help='snapshot one Kroger store\'s prices')
    prices.add_argument('--zip', dest='zip_code', required=True)
    prices.add_argument('--location-id', help='skip the location lookup')
    prices.add_argument('--catalogue', default=str(root / 'skills/meals/catalogue.json'))
    prices.add_argument('--env', default=str(root / '.env'))
    prices.add_argument('--out-dir', default=str(root / 'skills/meals'))

    args = parser.parse_args(argv)
    try:
        if args.command == 'stores':
            if args.region:
                found = fetch_region(http, args.region)
                path = write_snapshot(args.out, {'stores': found, 'region': text_code(args.region)},
                                      source='OpenStreetMap via Overpass', licence='ODbL 1.0')
                brands = len({store['brand'] for store in found if store.get('brand')})
                print(f'{len(found)} stores across {text_code(args.region)} '
                      f'({brands} brands) -> {path}')
                return 0
            if args.lat is None or args.lon is None:
                raise ValueError('give --region for a state, or --lat and --lon for a radius')
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

        if args.command == 'nutrition':
            terms = catalogue_terms(args.catalogue)
            snapshot = fetch_nutrition(http, args.api_key or usda_key(args.env), terms)
            path = write_snapshot(args.out, snapshot, source='USDA FoodData Central',
                                  licence='public domain (US government work)')
            matched = sum(1 for row in snapshot['items'].values() if row)
            print(f'{matched}/{len(terms)} ingredients matched -> {path}')
            if snapshot['unmatched']:
                print('no match: ' + ', '.join(snapshot['unmatched']), file=sys.stderr)
            if snapshot['stopped']:
                # Say what stopped it and what is missing. There is no resume:
                # a re-run starts from the top and re-fetches what is already here.
                print(f"stopped at {snapshot['stopped']}", file=sys.stderr)
                print(f"{len(snapshot['pending'])} not looked up: "
                      + ', '.join(snapshot['pending'][:8])
                      + ('...' if len(snapshot['pending']) > 8 else ''), file=sys.stderr)
                print('a free key at fdc.nal.usda.gov/api-key-signup.html lifts the '
                      'DEMO_KEY limit; re-run to fetch the rest', file=sys.stderr)
            return 0

        client_id, client_secret = credentials(args.env)
        token = kroger_token(http, client_id, client_secret)
        # The store is looked up whichever way the id arrived. A snapshot that
        # knows only a location id can price a basket at a shop it cannot name,
        # place on a map, or say the closing time of -- which is most of what
        # somebody collecting the shopping actually needs.
        nearby = kroger_locations(http, token, args.zip_code, limit=25)
        location_id = args.location_id
        if not location_id:
            if not nearby:
                raise ValueError(f'no Kroger-family store near {args.zip_code}')
            location_id = nearby[0]['location_id']
            print(f"using {nearby[0]['name']} ({location_id})")
        store = next((row for row in nearby if row['location_id'] == location_id), None)
        terms = catalogue_terms(args.catalogue)
        snapshot = fetch_prices(http, client_id, client_secret, location_id, terms)
        # None when --location-id names a store outside this postcode's results:
        # an admitted gap, not a store record invented to fill the field.
        snapshot['store'] = store
        # Whose shops these are. Kroger prices US stores, and a reader elsewhere
        # needs to be told that rather than shown a number about the wrong country.
        snapshot['country'] = 'US'
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
