"""Contract for putting the week's shopping in somebody's own Kroger cart.

Kroger's cart API only adds. It cannot read the cart and cannot check out, so the
person still chooses the time and pays in the Kroger or Ralphs app. What is
tested is what could go wrong on the way there:

* a login finished in the wrong conversation, or long after it was started;
* a login code or token echoed into a chat;
* the same list added twice, doubling everything in the cart;
* a package count that buys four bags when one holds four avocados;
* a line with no store product sent as though it had one.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.parse

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/meals/scripts'
SCRIPT = SCRIPTS / 'cart.py'
MEALS = SCRIPTS / 'meals.py'
sys.path.insert(0, str(SCRIPTS))
import cart  # noqa: E402

REDIRECT = 'https://lusknchars.github.io/meals-planner/kroger/'
NOW = 1_790_000_000
CATALOGUE = {'currency': 'USD', 'venues': [], 'categories': {}, 'recipes': [
    {'id': 'avo', 'title': 'Avocados', 'slot': 'breakfast', 'calories': 500, 'cost': 5.0,
     'tags': [], 'ingredients': [{'item': 'avocado', 'quantity': 5, 'unit': 'un', 'cost': 5.0}]},
    {'id': 'chicken', 'title': 'Chicken', 'slot': 'lunch', 'calories': 600, 'cost': 6.0,
     'tags': [], 'ingredients': [{'item': 'chicken breast', 'quantity': 600, 'unit': 'g',
                                  'cost': 6.0}]},
    {'id': 'rice', 'title': 'Rice', 'slot': 'dinner', 'calories': 400, 'cost': 1.0,
     'tags': [], 'ingredients': [{'item': 'rice', 'quantity': 100, 'unit': 'g', 'cost': 1.0}]}]}
PRICES = {
    'captured': '2026-09-16T04:00:00+00:00', 'location_id': '70300123',
    'store': {'name': 'Ralphs - Downtown San Diego', 'chain': 'RALPHS'},
    'items': {
        'avocado': [{'product_id': '0000000004046', 'description': 'Hass Avocados Bag',
                     'brand': 'Kroger', 'size': '4 ct', 'sold_by': 'UNIT', 'price': 4.99,
                     'promo': None, 'unit_price': 1.25, 'unit': 'ct', 'relevant': True}],
        'chicken breast': [{'product_id': '0000000094008', 'description': 'Chicken Breast',
                            'brand': None, 'size': '1 lb', 'sold_by': 'WEIGHT', 'price': 4.99,
                            'promo': None, 'unit_price': 11.0, 'unit': 'kg',
                            'relevant': True}]}}


def s256(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / 'catalogue.json').write_text(json.dumps(CATALOGUE))
        (self.home / 'prices.70300123.json').write_text(json.dumps(PRICES))
        self.env = {'HERMES_HOME': str(self.home),
                    'MEALS_CATALOGUE': str(self.home / 'catalogue.json'),
                    'MEALS_PRICES_DIR': str(self.home),
                    'MEALS_NUTRITION': str(self.home / 'absent.json')}
        patched = mock.patch.dict(os.environ, self.env)
        patched.start()
        self.addCleanup(patched.stop)
        self.calls = []
        self.token_status = 200
        self.cart_status = 204

    def meals(self, *args, scope='chat-1'):
        run = subprocess.run([sys.executable, str(MEALS), '--scope', scope, *args],
                             env={**os.environ, **self.env}, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def planned(self, people='1', *extra, scope='chat-1'):
        self.meals('profile', 'set', '--people', people, '--calories', '2000', '--diet', 'none',
                   '--store', '70300123', *extra, scope=scope)
        self.meals('plan', '--days', '1', '--start', '2026-09-21', scope=scope)

    def fetch(self, method, url, headers=None, body=None, timeout=None):
        sent = body.decode() if body else ''
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'body': sent})
        if url.endswith('/connect/oauth2/token'):
            reply = {'access_token': f'access-{len(self.calls)}', 'refresh_token': 'refresh-1',
                     'expires_in': 1800, 'token_type': 'bearer'}
            return self.token_status, json.dumps(reply).encode()
        return self.cart_status, b''

    def connect(self, scope='chat-1', now=NOW):
        found = cart.login_url(scope, 'client-1', REDIRECT, now=now)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(found['login_url']).query))
        return found, query

    def link(self, state, scope='chat-1', now=NOW + 60, code='the-code'):
        return cart.link(self.fetch, scope, f'kroger:{state}.{code}', 'client-1', 'secret-1',
                         REDIRECT, now=now)

    def connected(self, scope='chat-1'):
        _, query = self.connect(scope)
        self.link(query['state'], scope)
        self.calls.clear()

    def add(self, scope='chat-1', modality='PICKUP', now=NOW + 120):
        return cart.add(self.fetch, scope, '2026-09-21', modality, 'client-1', 'secret-1',
                        now=now)

    def form(self, call):
        return dict(urllib.parse.parse_qsl(call['body']))


class Login(Base):
    def test_the_login_link_asks_kroger_for_cart_access_with_pkce(self):
        found, query = self.connect()
        self.assertTrue(found['login_url'].startswith(
            'https://api.kroger.com/v1/connect/oauth2/authorize?'))
        self.assertEqual(query['client_id'], 'client-1')
        self.assertEqual(query['redirect_uri'], REDIRECT)
        self.assertEqual(query['scope'], 'cart.basic:write')
        self.assertEqual(query['response_type'], 'code')
        self.assertEqual(query['code_challenge_method'], 'S256')
        self.assertTrue(query['state'])

    def test_the_pasted_code_is_exchanged_with_the_matching_verifier(self):
        _, query = self.connect()
        found = self.link(query['state'])
        self.assertEqual(found, {'connected': True})
        call = self.calls[0]
        self.assertEqual(call['url'], 'https://api.kroger.com/v1/connect/oauth2/token')
        expected = base64.b64encode(b'client-1:secret-1').decode()
        self.assertEqual(call['headers']['Authorization'], f'Basic {expected}')
        form = self.form(call)
        self.assertEqual(form['grant_type'], 'authorization_code')
        self.assertEqual(form['code'], 'the-code')
        self.assertEqual(form['redirect_uri'], REDIRECT)
        self.assertEqual(s256(form['code_verifier']), query['code_challenge'])

    def test_a_code_from_another_conversation_is_refused(self):
        _, query = self.connect('chat-1')
        with self.assertRaises(ValueError):
            self.link(query['state'], scope='chat-2')
        with self.assertRaises(ValueError):
            self.link('0' * len(query['state']), scope='chat-1')
        self.assertEqual(self.calls, [], 'nothing is sent to Kroger for a mismatched login')

    def test_a_code_with_characters_no_login_produces_is_refused(self):
        _, query = self.connect()
        for code in ("abc'; rm -rf ~; echo '", 'abc def', 'abc\nnext', 'abc$(id)'):
            with self.assertRaises(ValueError, msg=code):
                self.link(query['state'], code=code)
        self.assertEqual(self.calls, [])

    def test_a_login_finished_after_ten_minutes_is_refused(self):
        _, query = self.connect()
        with self.assertRaises(ValueError) as caught:
            self.link(query['state'], now=NOW + 11 * 60)
        self.assertIn('connect', str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_a_refused_code_is_not_repeated_and_leaves_nothing_connected(self):
        _, query = self.connect()
        self.token_status = 400
        with self.assertRaises(ValueError) as caught:
            self.link(query['state'], code='secret-code')
        self.assertIn('400', str(caught.exception))
        self.assertNotIn('secret-code', str(caught.exception))
        self.planned()
        with self.assertRaises(ValueError):
            self.add()


class Adding(Base):
    def test_nothing_is_added_before_connecting(self):
        self.planned()
        with self.assertRaises(ValueError) as caught:
            self.add()
        self.assertIn('connect', str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_the_priced_products_go_in_by_the_package(self):
        self.planned()
        self.connected()
        found = self.add()
        call = self.calls[0]
        self.assertEqual((call['method'], call['url']),
                         ('PUT', 'https://api.kroger.com/v1/cart/add'))
        self.assertEqual(call['headers']['Authorization'], 'Bearer access-1')
        items = {item['upc']: item for item in json.loads(call['body'])['items']}
        # Five avocados from bags of four is two bags, not five.
        self.assertEqual(items['0000000004046'], {'upc': '0000000004046', 'quantity': 2,
                                                  'modality': 'PICKUP'})
        self.assertEqual(items['0000000094008']['quantity'], 2, '600 g in 1 lb packs')
        self.assertEqual(found['check_in_app'], ['chicken breast'], 'sold by weight')
        self.assertEqual(found['store'], 'Ralphs - Downtown San Diego')
        self.assertNotIn('access-1', json.dumps(found))

    def test_a_line_with_no_store_product_is_named_not_sent(self):
        self.planned()
        self.connected()
        found = self.add()
        self.assertEqual(found['not_added'], ['rice'])
        self.assertEqual(len(json.loads(self.calls[0]['body'])['items']), 2)

    def test_the_same_list_twice_adds_nothing_the_second_time(self):
        self.planned()
        self.connected()
        self.add()
        self.calls.clear()
        again = self.add()
        self.assertEqual(self.calls, [], 'a second add would double the cart')
        self.assertEqual(again['added'], [])
        self.assertEqual(sorted(again['already_in_cart']), ['avocado', 'chicken breast'])

    def test_a_bigger_list_adds_only_the_difference(self):
        self.planned()
        self.connected()
        self.add()
        self.planned('2')
        self.calls.clear()
        self.add()
        items = {item['upc']: item['quantity'] for item in json.loads(self.calls[0]['body'])['items']}
        self.assertEqual(items, {'0000000004046': 1, '0000000094008': 1})

    def test_an_expired_token_is_refreshed_before_adding(self):
        self.planned()
        self.connected()
        self.add(now=NOW + 3 * 3600)
        self.assertEqual(self.form(self.calls[0])['grant_type'], 'refresh_token')
        self.assertEqual(self.calls[1]['headers']['Authorization'], 'Bearer access-1')

    def test_a_refused_refresh_asks_for_a_new_login(self):
        self.planned()
        self.connected()
        self.token_status = 400
        with self.assertRaises(ValueError) as caught:
            self.add(now=NOW + 3 * 3600)
        self.assertIn('connect', str(caught.exception))
        self.token_status = 200
        self.calls.clear()
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(self.calls, [], 'the dead login was dropped')

    def test_only_pickup_or_delivery(self):
        self.planned()
        self.connected()
        with self.assertRaises(ValueError):
            self.add(modality='SHIP')

    def test_a_number_outside_the_united_states_gets_no_cart(self):
        self.planned('1', '--phone', '+55 11 91234 5678')
        self.connected()
        with self.assertRaises(ValueError) as caught:
            self.add()
        self.assertIn('BR', str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_disconnecting_drops_the_login(self):
        self.planned()
        self.connected()
        self.assertEqual(cart.disconnect('chat-1'), {'disconnected': True})
        with self.assertRaises(ValueError):
            self.add()


class CommandLine(unittest.TestCase):
    def test_missing_kroger_credentials_exit_nonzero_with_json_on_stderr(self):
        with tempfile.TemporaryDirectory() as home:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith('KROGER_')}
            run = subprocess.run([sys.executable, str(SCRIPT), '--scope', 'chat-1', 'connect'],
                                 env={**env, 'HERMES_HOME': home}, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn('KROGER_CLIENT_ID', json.loads(run.stderr)['error'])


if __name__ == '__main__':
    unittest.main()
