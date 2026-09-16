#!/usr/bin/env python3
"""Reading a shelf: what a product is, how much of it you get, and what that costs.

A store comparison that takes the lowest sticker price ranks whoever stocks the
smallest packet. Live, milk matched a 16 fl oz single-serve at $1.29 -- $2.73 a
litre against a gallon's $1.00 -- and chicken breast matched deli slices at
$17.60 a kilo against raw breast at $8.81. Both looked cheap and neither was.

So a staple names a standard quantity, every candidate is normalised to it, and a
product that is not the staple cannot price it at all.

Standard library only, no network. Size parsing and relevance also exist in
refresh.py, which is not in the agent's image; this is the copy the shipped
scripts share, and the two should be unified when refresh.py moves in with them.
"""
import re

# Everything resolves to one of these, so two stores can be compared at all.
WEIGHT_KG = {'oz': 0.0283495, 'lb': 0.453592, 'g': 0.001, 'kg': 1.0}
VOLUME_L = {'fl oz': 0.0295735, 'gal': 3.78541, 'qt': 0.946353,
            'pt': 0.473176, 'ml': 0.001, 'l': 1.0, 'liter': 1.0, 'litre': 1.0}
COUNT_UNITS = {'ct', 'count', 'each', 'ea', 'dozen'}
SIZE_RE = re.compile(r'^\s*(\d+(?:\.\d+)?|\d+\s*/\s*\d+)\s*'
                     r'(fl\s*oz|oz|lb|gal|qt|pt|ml|l|kg|g|ct|count|each|ea|dozen)\s*$',
                     re.IGNORECASE)

# Forms that are not the raw staple, however much their name matches. "Deli
# Fresh Rotisserie Seasoned Chicken Breast" contains "chicken breast" and is
# sliced lunch meat.
REJECT_FORMS = ('deli', 'sliced', 'lunch meat', 'luncheon', 'canned', 'dried',
                'dehydrated', 'powder', 'flavored', 'flavoured', 'snack', 'chips',
                'candy', 'cereal bar', 'baby food', 'babyfood', 'pet', 'dog', 'cat',
                'substitute', 'imitation', 'seasoned', 'marinated', 'breaded',
                'nuggets', 'patties', 'smoothie', 'juice', 'shake', 'creamer')
# Words describing a cut or state rather than the food itself.
GENERIC_WORDS = ('breast', 'fillet', 'fillets', 'fresh', 'whole', 'raw', 'large',
                 'medium', 'small', 'boneless', 'skinless')


def parse_size(value):
    """('1 gal') -> (1.0, 'gal'). None when it cannot be read, never a guess."""
    if not isinstance(value, str):
        return None
    match = SIZE_RE.match(value)
    if not match:
        return None
    raw, unit = match.group(1), re.sub(r'\s+', ' ', match.group(2).lower())
    if unit in COUNT_UNITS:
        amount_per = 12.0 if unit == 'dozen' else 1.0
        unit = 'ct'
    else:
        amount_per = 1.0
    if '/' in raw:
        top, bottom = (part.strip() for part in raw.split('/'))
        try:
            amount = float(top) / float(bottom)
        except (ValueError, ZeroDivisionError):
            return None
    else:
        amount = float(raw)
    return amount * amount_per, unit


def unit_price(price, size):
    """Price per litre, per kilo or per item. None when the size will not parse."""
    parsed = parse_size(size)
    if not parsed or not price:
        return None
    amount, unit = parsed
    if not amount:
        return None
    if unit == 'ct':
        return round(price / amount, 4), 'ct'
    if unit in WEIGHT_KG:
        return round(price / (amount * WEIGHT_KG[unit]), 4), 'kg'
    if unit in VOLUME_L:
        return round(price / (amount * VOLUME_L[unit]), 4), 'l'
    return None


def _stem(word):
    if len(word) > 3 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 3 and word.endswith('es'):
        return word[:-2]
    if len(word) > 3 and word.endswith('s'):
        return word[:-1]
    return word


def is_staple(term, description):
    """Whether this product is the staple asked for, by name and by form."""
    text = (description or '').lower()
    if not text:
        return False
    words = [word for word in re.split(r'[^a-z0-9]+', (term or '').lower()) if word]
    if any(form in text for form in REJECT_FORMS
           if not any(form in word for word in words)):
        return False
    required = [word for word in words if word not in GENERIC_WORDS] or words
    return all(_stem(word) in text or word in text for word in required)


def best_for(staple, rows):
    """The cheapest product that really is this staple, costed for its quantity.

    ``staple`` is {'term', 'quantity', 'unit'} -- a gallon of milk, a dozen eggs,
    a kilo of rice. Returns None when nothing qualifies, which is a real answer:
    a store that cannot price a staple should not be ranked as if it could.
    """
    best = None
    for row in rows or []:
        description = row.get('description')
        if not is_staple(staple['term'], description):
            continue
        item = (row.get('items') or [{}])[0]
        price = item.get('price') or {}
        regular, promo = price.get('regular'), price.get('promo')
        paid = promo if promo else regular
        measured = unit_price(paid, item.get('size'))
        if not measured or measured[1] != staple['unit']:
            continue
        rate, unit = measured
        found = {'term': staple['term'], 'product_id': row.get('productId'),
                 'description': description, 'brand': row.get('brand'),
                 'size': item.get('size'), 'price': paid, 'promo': promo or None,
                 'unit_price': round(rate, 2), 'unit': unit,
                 'quantity': staple['quantity'],
                 'cost': round(rate * staple['quantity'], 2)}
        if best is None or found['cost'] < best['cost']:
            best = found
    return best
