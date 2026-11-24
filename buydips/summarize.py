"""An optional written summary of the performance figures, produced by Claude.

The model only ever sees the numbers we computed, as plain text, and it only rewords them. Anything it returns is
checked before a person sees it: no figure that wasn't in the input, nothing that sounds like advice or a forecast,
a short length. If the key is missing, the service is slow or down, or a check fails, the page uses the plain
template sentence from performance.py instead. A failure here can never break the page."""

import logging
import re
from typing import Optional, Tuple

from . import config, db, performance

log = logging.getLogger("buyDips.summarize")

SYSTEM = (
    "You write a short, neutral description of how a person's followed stocks have performed, using ONLY the facts in "
    "the user's message. Write two sentences at most, in plain English. Use only figures that appear in the facts, "
    "copied exactly. Do not predict, do not say what anyone should do, and do not say buy, sell or hold. Do not say "
    "whether a price is cheap, expensive, strong or weak as an investment, and do not comment on what may happen next. "
    "Describe what already happened, nothing else."
)

# Words that make a description sound like advice, a forecast or a sales pitch.
_ADVICE = re.compile(
    r"\b(buy|buying|sell|selling|hold|holding|accumulate|invest|investing|investment|should|recommend\w*|advis\w*|"
    r"target|targets|undervalued|overvalued|bargain|opportunit\w*|outlook|forecast\w*|expect\w*|likely|unlikely|"
    r"bullish|bearish|upside|downside|momentum|rally|rebound|recover\w*|will|would|could|may|might|going to|"
    r"poised|set to|promising|attractive|safe|risky|risk|best|worst bet)\b", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
MAX_CHARS = 420


def ok(text: str, facts: str) -> bool:
    """True only if `text` is safe to show: short, one paragraph, no advice words, no link, and every number in it
    already appears in `facts`."""
    t = (text or "").strip()
    if not t or len(t) > MAX_CHARS or "\n" in t or "http" in t.lower() or "@" in t:
        return False
    if _ADVICE.search(t):
        return False
    known = set(_NUMBER.findall(facts))
    return all(n in known for n in _NUMBER.findall(t))


def _client():
    import anthropic

    # Short timeout and no retries: this runs while a page loads, and the template is an instant fallback.
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=8.0, max_retries=0)


def _ask(facts: str) -> Optional[str]:
    """One call to Claude. Returns the text, or None for any problem."""
    try:
        response = _client().messages.create(
            model=config.SUMMARY_MODEL,
            max_tokens=300,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": facts}],
        )
    except Exception as exc:  # noqa: BLE001  (timeouts, rate limits, outages, a missing package: all mean "use the template")
        log.warning("summary request failed: %s: %s", type(exc).__name__, exc)
        return None
    if response.stop_reason != "end_turn":      # includes "refusal" and "max_tokens"
        log.info("summary not used: stop_reason=%s", response.stop_reason)
        return None
    return next((b.text for b in response.content if b.type == "text"), "").strip() or None


def get(user_id: int, model: dict) -> Tuple[str, bool]:
    """(summary text, written_by_ai). Plain template unless a checked, written summary is available."""
    template = performance.template_summary(model)
    facts = performance.facts(model)
    if not template or not config.ANTHROPIC_API_KEY:
        return template, False
    digest = performance.digest(model)
    try:
        cached = db.get_summary(user_id, digest)
        if cached and ok(cached, facts):
            return cached, True
        if db.summaries_today(user_id) >= config.SUMMARY_PER_USER_PER_DAY or db.summaries_today() >= config.SUMMARY_PER_DAY_TOTAL:
            return template, False                       # spending limits: never more calls than these
        text = _ask(facts)
        if text and ok(text, facts):
            db.save_summary(user_id, digest, text)
            return text, True
        log.info("written summary rejected or unavailable; using the template")
    except Exception as exc:  # noqa: BLE001
        log.warning("summary unavailable: %s", exc)
    return template, False
