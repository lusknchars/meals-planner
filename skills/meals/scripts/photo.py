#!/usr/bin/env python3
"""Fetch one product photo so it can be attached, not linked.

The line does not preview links: a URL arrives as blue text and opens a
thumbnail. The platform does upload local files, so a photo is fetched here and
sent as a real attachment.

This is the only part of the skill that touches the network while answering.
meals.py stays offline and deterministic; this fetches one image on request.

Two rules carry the safety of it:

* The URL is BUILT from a product ID and never taken from input. An agent that
  could be told to fetch an arbitrary address would be a way to pull anything
  into somebody's chat.
* The ID must appear in that store's price snapshot, so only a product actually
  priced for this person can be fetched.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request

KROGER_IMAGE = 'https://www.kroger.com/product/images/{size}/front/{product_id}'
SIZES = ('small', 'medium', 'large', 'xlarge')
# xlarge is about 100 KB against medium's 6 KB, and the difference is visible:
# medium arrives as a soft thumbnail nobody wants to look at.
DEFAULT_SIZE = 'xlarge'
MAX_BYTES = 8 * 1024 * 1024
JPEG_MAGIC = b'\xff\xd8\xff'
USER_AGENT = 'meals-planner/1.0 (product photo)'


def image_url(product_id, size=DEFAULT_SIZE):
    """The public Kroger image address for one product. Built, never supplied."""
    if not isinstance(product_id, str) or not re.fullmatch(r'\d{6,20}', product_id):
        raise ValueError('product id must be digits, as Kroger publishes them')
    if size not in SIZES:
        raise ValueError('size must be one of: ' + ', '.join(SIZES))
    return KROGER_IMAGE.format(size=size, product_id=product_id)


def http(method, url, headers=None, timeout=30):
    """Returns (status, bytes). One more byte than the cap is read, so an
    oversized body is caught rather than streamed to disk."""
    request = urllib.request.Request(url, method=method,
                                     headers={'User-Agent': USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(2048)


def snapshot_dir(prices_dir=None):
    return Path(prices_dir or os.environ.get('MEALS_PRICES_DIR')
                or Path(__file__).resolve().parents[1])


def priced_product(store, product_id, prices_dir=None):
    """The snapshot row for this product, or a refusal naming why."""
    path = snapshot_dir(prices_dir) / f'prices.{store}.json'
    try:
        snapshot = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise ValueError(f'no usable price snapshot for store {store} at {path}')
    for rows in (snapshot.get('items') or {}).values():
        for row in rows or []:
            if row.get('product_id') == product_id:
                return row
    raise ValueError(f'product {product_id} is not in store {store}\'s price snapshot')


def fetch_photo(fetch, store, product_id, size=DEFAULT_SIZE, home=None, prices_dir=None):
    """Fetch one product photo into the Hermes cache and describe what it is.

    The cache is a root the delivery policy trusts unconditionally, which is why
    the file lands there rather than in a temporary directory.
    """
    row = priced_product(store, product_id, prices_dir)
    url = image_url(product_id, size)
    base = home or os.environ.get('HERMES_HOME')
    if not base:
        raise ValueError('HERMES_HOME must name this agent installation')
    folder = Path(base) / 'cache' / 'meals-photos'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / f'{product_id}-{size}.jpg'

    def described(byte_count, cached):
        return {'path': str(target), 'product': row.get('description'),
                'brand': row.get('brand'), 'price': row.get('price'),
                'bytes': byte_count, 'size': size, 'cached': cached, 'source': url}

    if target.is_file() and target.stat().st_size:
        return described(target.stat().st_size, True)

    status, payload = fetch('GET', url, headers={'User-Agent': USER_AGENT}, timeout=30)
    if status // 100 != 2:
        raise ValueError(f'Kroger answered {status} for that photo')
    if len(payload) > MAX_BYTES:
        raise ValueError(f'that photo is too large ({len(payload)} bytes)')
    if not payload.startswith(JPEG_MAGIC):
        raise ValueError('that was not a JPEG image, so nothing was kept')
    handle, temporary = tempfile.mkstemp(dir=str(folder))
    try:
        with os.fdopen(handle, 'wb') as file:
            file.write(payload)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return described(len(payload), False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, help='the Kroger locationId that was priced')
    parser.add_argument('--product-id', dest='product_id', required=True,
                        help='from a shopping line, not from a message')
    parser.add_argument('--size', default=DEFAULT_SIZE, choices=SIZES)
    args = parser.parse_args(argv)
    try:
        found = fetch_photo(http, args.store, args.product_id, size=args.size)
        json.dump(found, sys.stdout)
        return 0
    except (OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ValueError) else \
            f'{type(error).__name__}: {error}'
        json.dump({'error': message}, sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
