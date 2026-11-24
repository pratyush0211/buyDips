"""Share cards: a portrait image for WhatsApp, Instagram and the phone's share sheet, and a landscape one for link
previews. Drawn with Pillow at double size and scaled down, so edges are smooth. Shows only what the owner chose
to share: the bucket's name and stocks, and (if switched on) its average since added. Never prices or emails."""

import io
import threading
from pathlib import Path
from typing import Dict

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).parent / "card_fonts"
PAPER, INK, MUTED, LINE = "#f7f6f1", "#11201e", "#5c6764", "#e3e0d6"
TEAL, WHITE, SOFT = "#0f766e", "#ffffff", "#e1f1ed"
UP, DOWN = "#166534", "#c62828"
SIZES = {"story": (1080, 1350), "link": (1200, 630)}
S = 2  # draw at 2x, then scale down

_font_cache: Dict[tuple, ImageFont.FreeTypeFont] = {}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONTS / name), size * S)
    return _font_cache[key]


_missing: Dict[str, bytes] = {}


def printable(text: str, name: str = "InstrumentSerif-Regular.ttf") -> str:
    """Drop characters the card font has no glyph for (emoji, Devanagari...), which would draw as empty boxes.
    Falls back to "My bucket" if nothing is left."""
    f = font(name, 40)
    if name not in _missing:
        _missing[name] = bytes(f.getmask("\U0010fffe"))  # what a missing glyph looks like
    keep = "".join(c for c in text if c.isspace() or bytes(f.getmask(c)) != _missing[name])
    keep = " ".join(keep.split())
    return keep or "My bucket"


def _fit(draw, text: str, name: str, size: int, max_w: int, min_size: int):
    while size > min_size and draw.textlength(text, font=font(name, size)) > max_w * S:
        size -= 2
    return font(name, size)


def _ellipsis(draw, text: str, f, max_w: int) -> str:
    while len(text) > 1 and draw.textlength(text + "…", font=f) > max_w * S:
        text = text[:-1]
    return text + "…"


def _name_fit(draw, text: str, name: str, size: int, max_w: int, min_size: int):
    """A name that always fits: shrink first, then cut with an ellipsis."""
    f = _fit(draw, text, name, size, max_w, min_size)
    if draw.textlength(text, font=f) > max_w * S:
        text = _ellipsis(draw, text, f, max_w)
    return f, text


def _wrap(draw, text: str, name: str, size: int, max_w: int, lines: int = 2):
    """Break a title over up to `lines` lines, shrinking the type if it still doesn't fit."""
    for sz in range(size, 47, -4):
        f = font(name, sz)
        out, cur = [], ""
        for w in text.split():
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=f) <= max_w * S or not cur:
                cur = trial
            else:
                out.append(cur)
                cur = w
        out.append(cur)
        if len(out) <= lines and all(draw.textlength(ln, font=f) <= max_w * S for ln in out):
            return f, out
    f = font(name, 48)
    return f, [_ellipsis(draw, text, f, max_w)]


def _title(draw, text: str, y0: int, max_w: int, max_size: int, bottom: int, x: int) -> int:
    """Draw a serif title from y0, shrinking it until its measured extent ends above `bottom`. Returns that end."""
    for size in range(max_size, 47, -4):
        f, lines = _wrap(draw, text, "InstrumentSerif-Regular.ttf", size, max_w)
        sz = f.size / S
        first = y0 + sz * 0.8
        end = draw.textbbox((x * S, (first + (len(lines) - 1) * sz) * S), lines[-1], font=f, anchor="ls")[3] / S
        if end <= bottom:
            break
    base = first
    for ln in lines:
        _t(draw, (x, base), ln, f, INK, anchor="ls")
        base += sz
    return int(end)


def _t(draw, xy, text, f, fill, anchor="la", spacing=0):
    x, y = xy
    if spacing:  # letter-spaced caps
        for ch in text:
            draw.text((x * S, y * S), ch, font=f, fill=fill, anchor=anchor)
            x += draw.textlength(ch, font=f) / S + spacing
        return
    draw.text((x * S, y * S), text, font=f, fill=fill, anchor=anchor)


def _box(draw, x0, y0, x1, y1, r, fill, outline=None):
    draw.rounded_rectangle([x0 * S, y0 * S, x1 * S, y1 * S], radius=r * S, fill=fill, outline=outline, width=S if outline else 0)


def _logo(draw, x, y, size):
    _box(draw, x, y, x + size, y + size, size * 0.26, TEAL)
    p = lambda a, b: ((x + a * size / 32) * S, (y + b * size / 32) * S)  # noqa: E731
    draw.line([p(8.5, 11.5), p(14, 21.5), p(18.3, 15.4), p(24, 9.5)], fill=WHITE, width=int(size * 0.09 * S), joint="curve")


def _wordmark(draw, x, y, size):
    f1, f2 = font("InstrumentSerif-Regular.ttf", size), font("InstrumentSerif-Italic.ttf", size)
    draw.text((x * S, y * S), "buy", font=f1, fill=INK, anchor="ls")
    draw.text(((x + draw.textlength("buy", font=f1) / S) * S, y * S), "Dips", font=f2, fill=TEAL, anchor="ls")


def _chip(draw, x, y, label):
    f = font("Geist-Bold.ttf", 24)
    w = draw.textlength(label, font=f) / S + 28
    india = label == "IN"
    _box(draw, x - w, y, x, y + 42, 21, SOFT if india else "#eceaf6")
    _t(draw, (x - w / 2, y + 21), label, f, TEAL if india else "#4b4a8a", anchor="mm")


def _kicker(model: dict) -> str:
    return f"A BUCKET OF {model['n']} {'STOCK' if model['n'] == 1 else 'STOCKS'}"


def _avg_color(model: dict) -> str:
    return {"up": UP, "down": DOWN}.get(model.get("tone"), MUTED)


def _rows(d, stocks, x0, x1, y, row_h, count, name_w, size, min_size, name_x):
    """Draw `count` stock rows between hairlines; returns the y below them."""
    for i, st in enumerate(stocks[:count]):
        f, nm = _name_fit(d, st["name"], "Geist-Medium.ttf", size, name_w, min_size)
        _t(d, (name_x, y + row_h / 2), nm, f, INK, anchor="lm")
        _chip(d, x1, y + (row_h - 42) / 2, st["market"])
        if i < min(count, len(stocks)) - 1:
            d.line([(x0 * S, (y + row_h) * S), (x1 * S, (y + row_h) * S)], fill=LINE, width=S)
        y += row_h
    return y


def _story(model: dict) -> Image.Image:
    W, H = SIZES["story"]
    img = Image.new("RGB", (W * S, H * S), PAPER)
    d = ImageDraw.Draw(img)
    _logo(d, 72, 70, 64)
    _wordmark(d, 152, 120, 62)
    _t(d, (72, 236), _kicker(model), font("Geist-Bold.ttf", 28), TEAL, spacing=5)
    perf = bool(model.get("avg_text"))
    y = _title(d, model["name"], 268, W - 144, 112 if perf else 128, 590, 72) + 18
    if perf:
        _t(d, (72, y + 112), model["avg_text"], font("Geist-Bold.ttf", 132), _avg_color(model), anchor="ls")
        _t(d, (72, y + 158), "average since added", font("Geist-Medium.ttf", 32), MUTED)
        y += 200
    y += 14
    stocks = model["stocks"]
    row_h, floor = 68, H - 165
    room = floor - y - 34
    fit = max(1, int(room // row_h))
    if len(stocks) > fit:  # leave space for the "+ N more" line
        fit = max(1, int((room - 56) // row_h))
    extra = len(stocks) - min(fit, len(stocks))
    panel_h = 24 + row_h * min(fit, len(stocks)) + (56 if extra > 0 else 0) + 10
    _box(d, 56, y, W - 56, y + panel_h, 34, WHITE, LINE)
    ry = _rows(d, stocks, 96, W - 96, y + 24, row_h, fit, W - 330, 38, 28, 96)
    if extra > 0:
        _t(d, (96, ry + 28), f"+ {extra} more", font("Geist-Medium.ttf", 30), MUTED, anchor="lm")
    _t(d, (72, H - 112), "Make your own bucket at buydips.in", font("Geist-Bold.ttf", 34), INK)
    _t(d, (72, H - 64), "Not investment advice. For information only.", font("Geist-Regular.ttf", 26), MUTED)
    return img.resize((W, H), Image.LANCZOS)


def _link(model: dict) -> Image.Image:
    W, H = SIZES["link"]
    img = Image.new("RGB", (W * S, H * S), PAPER)
    d = ImageDraw.Draw(img)
    _logo(d, 64, 56, 56)
    _wordmark(d, 134, 102, 54)
    _t(d, (64, 186), _kicker(model), font("Geist-Bold.ttf", 24), TEAL, spacing=4)
    perf = bool(model.get("avg_text"))
    _title(d, model["name"], 214, 640, 100, H - 190 if perf else H - 110, 64)
    if perf:
        _t(d, (64, H - 96), model["avg_text"], font("Geist-Bold.ttf", 76), _avg_color(model), anchor="ls")
        _t(d, (64, H - 52), "average since added", font("Geist-Medium.ttf", 26), MUTED)
    else:
        _t(d, (64, H - 60), "India + US stocks, tracked on buyDips", font("Geist-Medium.ttf", 26), MUTED)
    _box(d, 760, 56, W - 56, H - 56, 30, WHITE, LINE)
    stocks = model["stocks"]
    fit = 6 if len(stocks) <= 6 else 5  # five rows plus a "+ N more" line when there are more
    ry = _rows(d, stocks, 796, W - 92, 80, 74, fit, 250, 30, 22, 796)
    if len(stocks) > fit:
        _t(d, (796, ry + 30), f"+ {len(stocks) - fit} more", font("Geist-Medium.ttf", 26), MUTED, anchor="lm")
    return img.resize((W, H), Image.LANCZOS)


_cache: Dict[str, bytes] = {}
_lock = threading.Lock()


def render(model: dict, kind: str = "story") -> bytes:
    """PNG bytes. `model`: name, n, stocks [{name, market}], optionally avg_text and tone."""
    model = {**model, "name": printable(model["name"])}
    key = repr((kind, model["name"], model["n"], tuple((s["name"], s["market"]) for s in model["stocks"]),
                model.get("avg_text"), model.get("tone")))
    with _lock:
        if key in _cache:
            return _cache[key]
    img = _story(model) if kind == "story" else _link(model)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = data
    return data
