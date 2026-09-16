"""meals-guard — the price rule as code instead of as advice.

Three times running, an agent whose persona and whose skill both said never to
state a price from memory answered a price question with Walmart, Whole Foods
and Aldi figures it had never looked up, and guessed the city on top. Written
rules are advice the model may or may not read: skill bodies load only when it
calls ``skill_view``, and even a persona line it does read is a line it can talk
itself past. This is a gate instead.

It watches the meals scripts run and remembers every number they printed. Then,
on the way out, it reads every money figure in the reply. A figure none of those
scripts produced was not looked up anywhere, so the reply does not go: it is
replaced by a short message that admits there is no shop price yet and asks
where the person actually is, which is the question that should have come first.

Numbers only count when they come from this skill's own scripts. Harvesting
every tool would let a web search launder exactly the figures the agent is meant
to refuse.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Only these produce a price this agent may state. A number from any other
# command is not a price, and a number from the web is the thing being refused.
PRICE_SCRIPTS = ("price.py", "compare.py", "meals.py")

# Tool that runs them. Its argument carrying the command line.
SHELL_TOOL = "terminal"
COMMAND_ARG = "command"

# A money figure in a reply: the symbol is what makes it a price claim, which is
# why a calorie count or a gram weight never matches. The optional tail catches
# "$10-16", where the second number is a price too.
#
# Four shapes, longest first so "1,234.56" is never read as the "1,23" of a
# Brazilian amount. Both conventions appear here for real: this agent answers
# Portuguese, and R$ 12,50 is twelve fifty, not twelve.
_AMOUNT = (
    r"\d{1,3}(?:\.\d{3})+,\d{1,2}"        # 1.234,56
    r"|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"  # 1,234.56
    r"|\d+,\d{1,2}"                       # 12,50
    r"|\d+(?:\.\d{1,2})?"                 # 12.50
)
_MONEY = re.compile(
    rf"(?:R\$|US\$|\$)\s*({_AMOUNT})"
    rf"(?:\s*[-\u2010-\u2015]\s*({_AMOUNT}))?"
)

# A comma with three digits after it separates thousands; with one or two, it is
# a decimal point. Nothing else distinguishes 1,500 from 1,50.
_COMMA_DECIMAL = re.compile(r"^\d{1,3}(?:\.\d{3})+,\d{1,2}$|^\d+,\d{1,2}$")

# Any number in a tool result. Script output is JSON, where a price is a bare
# number with no symbol in front of it.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

# A reply that opens a line with one of these sends a literal bullet character
# to somebody's phone; the escapes send a literal backslash.
_BULLET = re.compile(r"^[ \t]*[-*\u2022]\s+", re.MULTILINE)
_ESCAPED = re.compile(r"\\([-~*_`.!#()\[\]])")
# A dash used as a separator -- "1,830 kcal - $11.91" -- reads as punctuation
# nobody would type into a text message. Only a dash with space on BOTH sides
# qualifies, so "gluten-free" and "sugar-free" survive untouched, and the class
# is [ \t] rather than \s so this can never reach across a newline and weld two
# lines into one.
_SPACED_DASH = re.compile(r"[ \t]+[-\u2010-\u2015][ \t]+")

# Distinctive enough that none is an ordinary English word, so one hit is enough.
_PORTUGUESE = (
    "preço", "preços", "quanto", "custa", "mercado", "você", "está", "não",
    "semana", "compras", "arroz", "feijão", "leite", "comida", "refeição",
)

# A long result is usually a file dump, and scanning all of it buys nothing.
_MAX_SCAN = 200_000
# Enough for a long conversation's worth of prices without growing forever.
_MAX_FIGURES = 2_000

_sourced: Dict[str, Set[str]] = {}
_lock = threading.Lock()


def normalise(raw: Any) -> Optional[str]:
    """One number as a comparable string, or None when it is not a number.

    ``1.0``, ``1``, and ``"1,000.00"`` have to compare equal to what a reply
    writes as ``$1.00`` and ``$1,000``, so everything lands on two decimals.
    A Brazilian ``12,50`` is twelve fifty and must not round to twelve.
    """
    text = str(raw).strip().replace(" ", "")
    if _COMMA_DECIMAL.match(text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        return f"{float(text):.2f}"
    except (TypeError, ValueError):
        return None


def claimed_figures(text: str) -> Set[str]:
    """Every money figure a reply states."""
    found: Set[str] = set()
    for match in _MONEY.finditer(text or ""):
        for group in match.groups():
            if group is not None:
                value = normalise(group)
                if value is not None:
                    found.add(value)
    return found


def result_figures(result: Any) -> Set[str]:
    """Every number a script printed."""
    text = result if isinstance(result, str) else str(result or "")
    found: Set[str] = set()
    for match in _NUMBER.finditer(text[:_MAX_SCAN]):
        value = normalise(match.group(0))
        if value is not None:
            found.add(value)
    return found


def is_price_command(tool_name: str, args: Any) -> bool:
    """True when this tool call ran one of the meals scripts."""
    if (tool_name or "") != SHELL_TOOL or not isinstance(args, dict):
        return False
    command = str(args.get(COMMAND_ARG) or "")
    return any(script in command for script in PRICE_SCRIPTS)


def tidy(text: str) -> str:
    """Strip what reaches the reader as punctuation nobody typed."""
    without_bullets = _BULLET.sub("", text or "")
    return _SPACED_DASH.sub(", ", _ESCAPED.sub(r"\1", without_bullets))


def looks_portuguese(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in _PORTUGUESE)


def ask_for_location(portuguese: bool) -> str:
    """What to say instead of a price nobody looked up.

    It asks where they are, because that is the question a price answer needs
    and the one that got guessed. No bullets, no backslashes, short enough to
    read standing up.
    """
    if portuguese:
        return (
            "🛒 Ainda não tenho o preço de nenhuma loja para isso.\n\n"
            "Só falo preço que acabei de consultar numa loja de verdade, nunca de memória.\n\n"
            "Onde você está agora? Rua e bairro, ou cidade e estado, ou o CEP já basta.\n"
            "Aí eu consulto nas lojas perto de você."
        )
    return (
        "🛒 I do not have a shop price for that yet.\n\n"
        "I only give prices I just looked up at a real shop, never from memory.\n\n"
        "Where are you right now? Street and neighbourhood, or city and state, or a postcode.\n"
        "Then I will price it at the shops near you."
    )


def review(response_text: str, sourced: Set[str]) -> Optional[str]:
    """The whole decision, with no globals: the replacement, or None to leave it.

    A reply claiming any figure the scripts did not produce is replaced whole.
    Editing the offending line out would leave the rest of a fabricated answer
    standing, and the rest was fabricated too.
    """
    if not isinstance(response_text, str) or not response_text:
        return None
    unsourced = claimed_figures(response_text) - sourced
    if unsourced:
        logger.warning(
            "meals-guard: refused a reply claiming %s, which no meals script produced",
            ", ".join(sorted(unsourced)),
        )
        return ask_for_location(looks_portuguese(response_text))
    tidied = tidy(response_text)
    return tidied if tidied != response_text else None


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None,
                       session_id: Any = None, **_: Any) -> None:
    """Remember what the meals scripts printed, per conversation."""
    if not is_price_command(tool_name, args):
        return
    key = str(session_id or "")
    with _lock:
        figures = _sourced.setdefault(key, set())
        figures |= result_figures(result)
        if len(figures) > _MAX_FIGURES:
            _sourced[key] = set(list(figures)[-_MAX_FIGURES:])


def _on_transform_llm_output(response_text: str = "", session_id: Any = None,
                             **_: Any) -> Optional[str]:
    """Last stop before the reply leaves. Returning a string replaces it."""
    with _lock:
        sourced = set(_sourced.get(str(session_id or ""), ()))
    try:
        return review(response_text, sourced)
    except Exception:
        # A guard that throws must not eat the turn; it only ever declines to act.
        logger.debug("meals-guard: review failed, leaving the reply alone", exc_info=True)
        return None


def _on_session_ended(session_id: Any = None, **_: Any) -> None:
    with _lock:
        _sourced.pop(str(session_id or ""), None)


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("on_session_end", _on_session_ended)
    ctx.register_hook("on_session_reset", _on_session_ended)
