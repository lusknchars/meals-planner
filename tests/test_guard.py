"""Contract for the outgoing-price gate.

Three times running, an agent told in its persona and in its skill never to
state a price from memory answered "how much is milk and donuts" with Walmart,
Whole Foods, Aldi and Krispy Kreme figures, in markdown bullets, for a city it
had guessed. Instructions had been rewritten four times by then. These tests
pin the behaviour that does not depend on the model reading anything.

The rule: a money figure may leave only if one of this skill's own scripts
printed it in this conversation. Not the web, not another tool, not memory.
"""
import importlib.util
from pathlib import Path
import unittest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugin/meals-guard/__init__.py'
_spec = importlib.util.spec_from_file_location('meals_guard', PLUGIN)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


PRICED = '{"best": {"price": 3.79, "unit_price": 1.0, "size": "1 gal"}}'


class Figures(unittest.TestCase):
    def test_a_money_figure_is_read_from_a_reply(self):
        self.assertEqual(guard.claimed_figures('Whole gallon $3.79 today'), {'3.79'})

    def test_a_calorie_count_is_not_a_price(self):
        self.assertEqual(guard.claimed_figures('That is 2,728 kcal and 28.4 g fat'), set())

    def test_both_ends_of_a_range_are_prices(self):
        self.assertEqual(guard.claimed_figures('shop dozens run roughly $10-16'),
                         {'10.00', '16.00'})

    def test_thousands_and_bare_dollars_compare_with_script_output(self):
        self.assertEqual(guard.claimed_figures('$1,000 and $7'), {'1000.00', '7.00'})

    def test_reais_are_read_too(self):
        self.assertEqual(guard.claimed_figures('custa R$ 12,50 hoje'), {'12.50'})

    def test_a_script_result_yields_its_numbers(self):
        self.assertEqual(guard.result_figures(PRICED) & {'3.79', '1.00'}, {'3.79', '1.00'})


class WhichCommandsCount(unittest.TestCase):
    def test_a_meals_script_counts(self):
        self.assertTrue(guard.is_price_command(
            'terminal', {'command': 'python3 /opt/hermes/skills/meals/scripts/price.py --item milk'}))

    def test_another_shell_command_does_not(self):
        self.assertFalse(guard.is_price_command('terminal', {'command': 'curl https://example.com'}))

    def test_another_tool_does_not(self):
        self.assertFalse(guard.is_price_command('web_search', {'query': 'milk price walmart'}))


class Review(unittest.TestCase):
    def test_a_figure_no_script_produced_is_refused(self):
        reply = 'Milk (LA area): whole gallon ~$2.74 (Walmart+) up to ~$6.26 (Whole Foods)'
        replaced = guard.review(reply, {'3.79'})
        self.assertIsNotNone(replaced, 'a remembered Walmart figure must not go out')
        self.assertNotIn('2.74', replaced)
        self.assertNotIn('Walmart', replaced)

    def test_a_figure_a_script_produced_goes_out_unchanged(self):
        reply = '🥛 Milk, at Ralphs Fresh Fare\nWhole gallon $3.79, about $1.00 a litre'
        self.assertIsNone(guard.review(reply, {'3.79', '1.00'}))

    def test_one_bad_figure_among_good_ones_still_refuses(self):
        reply = 'Milk $3.79 at Ralphs. Donuts run about $15.99 a dozen.'
        self.assertIsNotNone(guard.review(reply, {'3.79'}))

    def test_a_reply_with_no_price_is_left_alone(self):
        self.assertIsNone(guard.review('Which city are you in?', set()))

    def test_the_refusal_asks_where_they_are(self):
        replaced = guard.review('milk is $2.74', set())
        for word in ('Street', 'city', 'postcode'):
            self.assertIn(word, replaced)

    def test_the_refusal_answers_portuguese_in_portuguese(self):
        replaced = guard.review('o leite custa R$ 8,90 no mercado', set())
        self.assertIn('Onde você está', replaced)

    def test_no_refusal_ever_sends_a_bullet_or_a_backslash(self):
        for portuguese in (True, False):
            message = guard.ask_for_location(portuguese)
            self.assertNotIn('\\', message)
            for line in message.splitlines():
                self.assertFalse(line.lstrip().startswith(('-', '*', '•')), line)


class Tidying(unittest.TestCase):
    def test_bullets_are_stripped_from_an_otherwise_good_reply(self):
        reply = '🛒 Shopping\n- avocado 4\n- banana 8'
        tidied = guard.review(reply, set())
        self.assertEqual(tidied, '🛒 Shopping\navocado 4\nbanana 8')

    def test_escaped_punctuation_is_unescaped(self):
        self.assertEqual(guard.tidy('about \\~2 kg and 3\\-4 days'), 'about ~2 kg and 3-4 days')

    def test_a_clean_reply_is_not_rewritten(self):
        self.assertIsNone(guard.review('🥛 Milk\nWhole gallon, ask me to price it', set()))


class PerConversation(unittest.TestCase):
    def setUp(self):
        guard._sourced.clear()
        self.addCleanup(guard._sourced.clear)

    def run_script(self, session, command=None, result=PRICED):
        guard._on_post_tool_call(
            tool_name='terminal',
            args={'command': command or 'python3 /opt/hermes/skills/meals/scripts/price.py --item milk'},
            result=result, session_id=session)

    def test_a_price_a_script_printed_may_be_stated(self):
        self.run_script('chat-a')
        self.assertIsNone(guard._on_transform_llm_output(
            response_text='Whole gallon $3.79', session_id='chat-a'))

    def test_a_price_from_another_command_may_not(self):
        self.run_script('chat-a', command='curl https://example.com/prices')
        self.assertIsNotNone(guard._on_transform_llm_output(
            response_text='Whole gallon $3.79', session_id='chat-a'))

    def test_one_household_never_answers_for_another(self):
        self.run_script('chat-a')
        self.assertIsNotNone(guard._on_transform_llm_output(
            response_text='Whole gallon $3.79', session_id='chat-b'),
            "another conversation's lookup is not this one's price")

    def test_a_price_stays_quotable_later_in_the_conversation(self):
        self.run_script('chat-a')
        guard._on_transform_llm_output(response_text='Whole gallon $3.79', session_id='chat-a')
        self.assertIsNone(guard._on_transform_llm_output(
            response_text='As I said, $3.79', session_id='chat-a'),
            'a figure this conversation looked up may be repeated')

    def test_ending_the_session_forgets_its_prices(self):
        self.run_script('chat-a')
        guard._on_session_ended(session_id='chat-a')
        self.assertIsNotNone(guard._on_transform_llm_output(
            response_text='Whole gallon $3.79', session_id='chat-a'))

    def test_a_guard_that_breaks_never_eats_the_turn(self):
        self.assertIsNone(guard._on_transform_llm_output(response_text=None, session_id='chat-a'))


class Registration(unittest.TestCase):
    def test_every_hook_the_manifest_declares_is_registered(self):
        registered = []

        class Ctx:
            def register_hook(self, name, callback):
                registered.append(name)

        guard.register(Ctx())
        self.assertEqual(sorted(registered), sorted(
            ['post_tool_call', 'transform_llm_output', 'on_session_end', 'on_session_reset']))


if __name__ == '__main__':
    unittest.main()
