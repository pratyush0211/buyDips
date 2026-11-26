"""The daily brief: once each morning, Claude researches the followed stocks that triggered a technical signal.

Spending is kept low on purpose:
  * Code finds the candidates (signals.py); a stock with nothing unusual costs nothing.
  * One small request per stock, shared by everyone who follows it, saved for the day. A stock is never researched twice.
  * The prompt is short, thinking is off, effort is low, searches are capped, the answer is a small JSON object.
  * A hard ceiling (BRIEF_MAX_PER_DAY) and a stop after repeated failures.

Nothing the model returns is trusted. Every point must cite a URL that really appeared in the search results, every
number must appear in the facts we gave it or in a cited passage, broker targets must appear in a cited passage, and
anything that reads as advice or a forecast is dropped. A brief that keeps no cited point, or scores below the bar, is
never shown, emailed or shareable."""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlparse

from . import catalog, charts, config, db

log = logging.getLogger("buyDips.briefs")

SYSTEM = (
    "You research one stock for a short digest. Use web search to find the latest news, results, broker "
    "coverage and media reports about it. Reply with ONLY a JSON object: "
    '{"material":0-10,"headline":"max 90 chars","points":[{"t":"max 150 chars","u":"source URL"}],'
    '"targets":[{"by":"broker","p":number,"u":"source URL"}]}. '
    "material is how significant the news is for a long-term holder; use 0 if nothing new in the last 3 days. "
    "Up to 3 points, each a fact already reported, taken from a page you found, with that page's URL. "
    "targets only if a named broker's target price appears in a source. "
    "No predictions, no advice, no buy/sell/hold, no opinion on whether the price is high or low."
)

# Words that make text sound like advice, a forecast or a pitch. (Broker targets live in their own field.)
_ADVICE = re.compile(
    r"\b(buy|buying|sell|selling|hold|holding|accumulate|invest|investing|investment|should|recommend\w*|advis\w*|"
    r"target|targets|undervalued|overvalued|bargain|opportunit\w*|outlook|forecast\w*|expect\w*|likely|unlikely|"
    r"bullish|bearish|upside|downside|rally|rebound|will|would|could|may|might|going to|poised|set to|"
    r"promising|attractive|safe|risky|must|don't miss|multibagger)\b", re.I)
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> Set[str]:
    return set(_NUM.findall(text.replace(",", "")))


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _check(text, max_len: int, known: Set[str]) -> Tuple[Optional[str], Optional[str]]:
    """(clean text, None) or (None, why it was refused)."""
    t = " ".join(text.split()) if isinstance(text, str) else ""
    if not t:
        return None, "empty"
    if len(t) > max_len:
        return None, "too_long"
    if _ADVICE.search(t):
        return None, "advice_words"
    if "http" in t.lower() or "@" in t:
        return None, "link"
    if not _numbers(t) <= known:
        return None, "unsupported_number"
    return t, None


def _clean(text: str, max_len: int, known: Set[str]) -> Optional[str]:
    return _check(text, max_len, known)[0]


# ---------------------------------------------------------------- choosing what to research

def candidates(followed: Dict[str, dict], dipped: Set[str] = frozenset()) -> List[str]:
    """Symbols worth researching today, most notable first, capped. `followed` is {symbol: perf row}. A stock needs a
    technical signal or to have dipped; everything else is skipped without any request."""
    scored = []
    for sym, p in followed.items():
        item = catalog.BY_SYMBOL.get(sym)
        if not item or item["type"] == "index":
            continue
        sigs = json.loads(p["signals"]) if p and p.get("signals") else []
        if not any(x["s"] >= config.BRIEF_MIN_SIGNAL for x in sigs) and sym not in dipped:
            continue                       # nothing strong enough to be worth reading the web for
        scored.append((-(sum(s["s"] for s in sigs) + (2 if sym in dipped else 0) + abs(p.get("r1m") or 0) / 10), sym))
    return [s for _, s in sorted(scored)][:config.BRIEF_MAX_PER_DAY]


def facts_for(sym: str, p: dict, consensus: Optional[dict]) -> str:
    """The compact facts the model gets: as few tokens as will do."""
    item = catalog.BY_SYMBOL[sym]
    cur = catalog.currency_symbol(sym)
    sigs = json.loads(p["signals"]) if p.get("signals") else []
    lines = [f"{item['name']} ({sym})", "Signals: " + ("; ".join(s["text"] for s in sigs) or "none")]
    moves = [f"{label} {p[k]:+.1f}%" for label, k in (("1M", "r1m"), ("3M", "r3m"), ("1Y", "r1y")) if p.get(k) is not None]
    if moves:
        lines.append("Moves: " + ", ".join(moves))
    if consensus and consensus.get("mean"):
        lines.append(f"Yahoo analyst mean target {cur}{consensus['mean']:g}")
    return "\n".join(lines)


# ---------------------------------------------------------------- asking Claude

def _client():
    import anthropic

    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=90.0, max_retries=1)


def _walk(node, urls: Set[str], cited: List[str]) -> None:
    """Collect every source URL and every quoted passage, whatever shape the search results come in."""
    if isinstance(node, dict):
        u = node.get("url")
        if isinstance(u, str) and u.startswith("https://"):
            urls.add(u)
        c = node.get("cited_text")
        if isinstance(c, str):
            cited.append(c)
        t = node.get("title")
        if isinstance(t, str) and isinstance(u, str):      # a search result's headline is quoted text we can check numbers against
            cited.append(t)
        for v in node.values():
            _walk(v, urls, cited)
    elif isinstance(node, list):
        for v in node:
            _walk(v, urls, cited)


USAGE = {"tokens_in": 0, "tokens_out": 0, "searches": 0}      # this run's totals, reported at the end of the job log


def _thinking_for(model: str) -> dict:
    # Haiku can be told not to think; the larger models answer without thinking and take "between_tools" instead.
    return {"type": "disabled"} if "haiku" in model else {"type": "between_tools"}


def _search_tool(model: str) -> dict:
    # The newer search tool (which filters pages with code) is only available on the larger models.
    kind = "web_search_20250305" if "haiku" in model else "web_search_20260209"
    return {"type": kind, "name": "web_search", "max_uses": config.BRIEF_MAX_SEARCHES}


def ask(facts: str) -> Optional[Tuple[str, Set[str], str]]:
    """One research request. Returns (reply text, source URLs, quoted passages) or None for any problem."""
    kw = dict(model=config.BRIEF_MODEL, max_tokens=900, thinking=_thinking_for(config.BRIEF_MODEL), output_config={"effort": "low"},
              system=SYSTEM, tools=[_search_tool(config.BRIEF_MODEL)], messages=[{"role": "user", "content": facts}])
    try:
        client = _client()
        try:
            r = client.messages.create(**kw)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "BadRequestError" or "thinking" not in str(exc):
                raise
            kw["thinking"] = {"type": "between_tools"} if kw["thinking"]["type"] == "disabled" else {"type": "disabled"}
            r = client.messages.create(**kw)      # one retry when the model wanted the other thinking setting
    except Exception as exc:  # noqa: BLE001  (timeouts, rate limits, an unavailable tool: all mean "no brief today")
        log.warning("brief request failed: %s: %s", type(exc).__name__, exc)
        return None
    u = getattr(r, "usage", None)
    searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    USAGE["tokens_in"] += getattr(u, "input_tokens", 0) or 0
    USAGE["tokens_out"] += getattr(u, "output_tokens", 0) or 0
    USAGE["searches"] += searches
    log.info("brief tokens: in=%s out=%s searches=%s stop=%s", getattr(u, "input_tokens", "?"), getattr(u, "output_tokens", "?"), searches, r.stop_reason)
    if r.stop_reason != "end_turn":                      # includes pause_turn, refusal, max_tokens
        return None
    text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
    urls: Set[str] = set()
    cited: List[str] = []
    _walk(r.model_dump(), urls, cited)
    return (text, urls, " ".join(cited)) if text else None


# ---------------------------------------------------------------- checking the answer

def parse(text: str) -> Optional[dict]:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        data = json.loads(t[a:b + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def validate(raw: dict, urls: Set[str], cited: str, facts: str, report: Optional[dict] = None) -> Optional[dict]:
    """The brief that is safe to keep, or None. Drops anything unsourced, invented or advice-like. If `report` is given
    it is filled with how many points were seen and kept and why the rest were dropped (for the job log)."""
    why: Dict[str, int] = {}
    known = _numbers(facts) | _numbers(cited)
    try:
        material = max(0, min(10, int(raw.get("material", 0))))
    except (TypeError, ValueError):
        if report is not None:
            report.update(material=None, why={"bad_material": 1})
        return None
    headline, h_why = _check(raw.get("headline"), 110, known)
    if h_why:
        why["headline_" + h_why] = 1
    seen = (raw.get("points") or [])[:3] if isinstance(raw.get("points"), list) else []
    points = []
    for p in seen:
        if not isinstance(p, dict) or p.get("u") not in urls:
            why["not_in_search_results"] = why.get("not_in_search_results", 0) + 1
            continue
        t, w = _check(p.get("t"), 180, known)
        if w:
            why[w] = why.get(w, 0) + 1
            continue
        points.append({"t": t, "u": p["u"], "d": _domain(p["u"])})
    targets = []
    for t in (raw.get("targets") or [])[:3] if isinstance(raw.get("targets"), list) else []:
        try:
            price = float(t["p"])
            by = " ".join(str(t["by"]).split())
        except (KeyError, TypeError, ValueError):
            continue
        if (t.get("u") in urls and 0 < price < 1e8 and 0 < len(by) <= 40 and re.fullmatch(r"[\w .&'-]+", by)
                and f"{price:g}" in cited.replace(",", "") and not _ADVICE.search(by)):
            targets.append({"by": by, "p": price, "u": t["u"], "d": _domain(t["u"])})
        else:
            why["target_unsupported"] = why.get("target_unsupported", 0) + 1
    if report is not None:
        report.update(material=material, seen=len(seen), kept=len(points), why=why, urls=len(urls))
    if not headline or not points:
        return None
    return {"material": material, "headline": headline, "points": points, "targets": targets}


# ---------------------------------------------------------------- the daily run

def run_daily(provider, now: Optional[datetime] = None, dipped: Set[str] = frozenset()) -> dict:
    """Research today's candidates once. Safe to call every hour: it does nothing before the morning run time, nothing
    once the day is complete, and picks up where it stopped if a run was cut short."""
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    out = {"researched": 0, "worthy": 0, "skipped": None}
    USAGE.update(tokens_in=0, tokens_out=0, searches=0)
    if not config.ANTHROPIC_API_KEY:
        out["skipped"] = "no key"
    elif now.hour < config.BRIEF_RUN_AFTER_UTC_HOUR:
        out["skipped"] = "too early"
    elif db.brief_run_done(day) and not config.BRIEF_FORCE:
        out["skipped"] = "done"
    if out["skipped"]:
        return out

    followed = {s["symbol"] for s in db.active_subscriptions()}
    perf = db.perf_for(followed)
    todo = [s for s in candidates({s: perf[s] for s in followed if s in perf}, dipped) if config.BRIEF_FORCE or not db.get_brief(s, day)]
    stocks = [s for s in todo if catalog.BY_SYMBOL[s]["type"] == "stock"]       # funds like gold ETFs have no analyst targets
    consensus = provider.analyst(stocks) if stocks and hasattr(provider, "analyst") else {}
    failures = 0
    started = time.monotonic()
    for sym in todo:
        if time.monotonic() - started > config.BRIEF_TIME_BUDGET_SECONDS:
            log.info("brief run paused at its time budget; the next run continues")
            out.update(USAGE)
            return out
        facts = facts_for(sym, perf[sym], consensus.get(sym))
        got = ask(facts)
        if got is None:
            failures += 1
            if failures >= config.BRIEF_MAX_FAILURES:
                log.warning("stopping the brief run after %d failed requests in a row; will try again next hour", failures)
                out.update(USAGE)
                return out
            continue
        failures = 0
        raw = parse(got[0])
        report: dict = {}
        brief = validate(raw, got[1], got[2], facts, report) if raw else None
        if raw is None:
            log.info("brief %s: reply was not valid JSON: %.120s", sym, got[0].replace("\n", " "))
        else:
            log.info("brief %s: material=%s points kept %s of %s, search results %s, dropped for: %s", sym, report.get("material"),
                     report.get("kept"), report.get("seen"), report.get("urls"), report.get("why") or "nothing")
        out["researched"] += 1
        worthy = bool(brief and brief["material"] >= config.BRIEF_MIN_MATERIAL)
        body = None
        if worthy:
            body = {"points": brief["points"], "targets": brief["targets"], "consensus": consensus.get(sym),
                    "signals": json.loads(perf[sym]["signals"]) if perf[sym].get("signals") else []}
            out["worthy"] += 1
        db.save_brief(sym, day, worthy, brief["material"] if brief else 0, brief["headline"] if worthy else None, body)
    db.save_brief(db.RUN_MARK, day, False, 0, None, None)
    out.update(USAGE)
    return out


# ---------------------------------------------------------------- showing a brief

def url(sym: str, day: str) -> str:
    return f"{config.BASE_URL}/brief/{day}/{quote(sym, safe='')}"


def _money(sym: str, x: float) -> str:
    return f"{catalog.currency_symbol(sym)}{x:,.0f}" if x >= 100 else f"{catalog.currency_symbol(sym)}{x:,.2f}"


def since_day(now: Optional[datetime] = None) -> str:
    """Briefs from today or yesterday count as current, so the page isn't empty before the morning run."""
    return ((now or datetime.now(timezone.utc)).date() - timedelta(days=1)).isoformat()


def view(sym: str, brief: dict, perf: Optional[dict]) -> dict:
    """Everything a page, a share card or an email needs about one brief."""
    item = catalog.BY_SYMBOL[sym]
    body = brief.get("body") or {}
    perf = perf or {}
    values = charts.closes(perf.get("series"))
    r1y = perf.get("r1y")
    cons = body.get("consensus") or {}
    consensus = None
    if cons.get("mean"):
        consensus = f"Yahoo analyst consensus {_money(sym, cons['mean'])}"
        if cons.get("low") and cons.get("high"):
            consensus += f" (range {_money(sym, cons['low'])} to {_money(sym, cons['high'])})"
    day = brief["day"]
    return {
        "symbol": sym, "name": item["name"], "market": "IN" if item["group"] == "India" else "US", "day": day,
        "day_label": datetime.fromisoformat(day).strftime("%a %d %b"), "material": brief.get("material", 0),
        "headline": brief["headline"], "points": body.get("points", []),
        "targets": [{**t, "price": _money(sym, t["p"])} for t in body.get("targets", [])],
        "consensus": consensus, "signals": [s["text"] for s in body.get("signals", [])],
        "values": values, "chart": charts.geometry(values), "tone": "up" if (r1y or 0) >= 0 else "down",
        "move": None if r1y is None else f"{r1y:+.1f}%", "url": url(sym, day), "path": f"/brief/{day}/{quote(sym, safe='')}",
    }
