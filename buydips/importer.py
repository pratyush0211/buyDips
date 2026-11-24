"""Turn a broker's holdings file (CSV or Excel) or pasted text into the catalog stocks it contains.

Only stock NAMES and symbols are used: quantities, prices and account details in the file are never read or kept.
Brokers lay their files out differently, so this finds the right columns itself, and anything it can't match is
reported back instead of silently dropped."""

import csv
import io
import re
import zipfile
from typing import Dict, List, Tuple
from xml.etree import ElementTree as ET

from . import catalog

MAX_BYTES = 1_000_000          # uploads larger than this are refused
MAX_ROWS = 5000
_MAX_XML = 20_000_000          # decompressed XML limit: protects against zip bombs
_STOP = {"ltd", "limited", "inc", "corp", "corporation", "co", "company", "the", "pvt", "private", "plc"}
_HEADER_WORDS = re.compile(r"symbol|scrip|ticker|instrument|stock|company|security|name", re.I)
_SKIP_ROWS = re.compile(r"^(grand\s+)?total|^net\b|^sub\s*total|^summary|^holdings?$|^equity$|^stocks?$|^mutual funds?$", re.I)
_SUFFIXES = re.compile(r"-(EQ|BE|BZ|SM|N1)$|\.(NS|BO|NSE|BSE)$", re.I)


class ImportError_(ValueError):
    """The file can't be read; the message is safe to show the user."""


def norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("&", " and "))
    return " ".join(w for w in t.split() if w not in _STOP)


def _build_indexes():
    by_name, by_symbol = {}, {}
    for item in catalog.ITEMS:
        if item["type"] not in catalog.TRADED or not catalog.offered(item):
            continue
        by_name.setdefault(norm(item["name"]), item)
        base = item["symbol"].split(".")[0].upper()
        by_symbol[base] = None if base in by_symbol else item      # None marks a symbol both markets use: ambiguous
    return by_name, by_symbol


_BY_NAME, _BY_SYMBOL = _build_indexes()


# ---------------- reading files ----------------

def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeError:
            continue
    return data.decode("latin-1")


def read_csv(data: bytes) -> List[List[str]]:
    text = _decode(data)
    lines = [l for l in text.splitlines() if l.strip()][:MAX_ROWS]
    if not lines:
        return []
    # the delimiter is whichever of , ; tab | appears most across the first lines
    sample = "\n".join(lines[:30])
    delim = max(",;\t|", key=sample.count)
    return [[c.strip() for c in row] for row in csv.reader(lines, delimiter=delim)]


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data: bytes) -> List[List[str]]:
    """A minimal reader for .xlsx (a zip of XML files): text and numbers only, which is all we need."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_("That doesn't look like an Excel (.xlsx) file.")
    names = zf.namelist()
    sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets or sum(zf.getinfo(n).file_size for n in sheets + ["xl/sharedStrings.xml"] if n in names) > _MAX_XML:
        raise ImportError_("We couldn't read that Excel file.")

    def parse(name: str):
        raw = zf.read(name)
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:        # no entity tricks in a spreadsheet
            raise ImportError_("We couldn't read that Excel file.")
        return ET.fromstring(raw)

    strings: List[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in parse("xl/sharedStrings.xml").iterfind(".//{*}si"):
            strings.append("".join(t.text or "" for t in si.iterfind(".//{*}t")))
    rows: List[List[str]] = []
    for sheet in sheets:
        for row in parse(sheet).iterfind(".//{*}row"):
            cells: Dict[int, str] = {}
            for c in row.findall("{*}c"):
                ref, kind = c.get("r") or "", c.get("t")
                if not ref:
                    continue
                if kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iterfind(".//{*}t"))
                else:
                    v = c.find("{*}v")
                    val = (v.text or "") if v is not None else ""
                    if kind == "s" and val.isdigit() and int(val) < len(strings):
                        val = strings[int(val)]
                cells[_col(ref)] = val.strip()
            if any(cells.values()):
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            if len(rows) >= MAX_ROWS:
                return rows
    return rows


def read_file(filename: str, data: bytes) -> List[List[str]]:
    if len(data) > MAX_BYTES:
        raise ImportError_("That file is too large. A holdings file is normally well under 1 MB.")
    if data[:2] == b"PK":                                     # .xlsx is a zip
        return read_xlsx(data)
    if data[:4] == b"%PDF":
        raise ImportError_("PDF files can't be read. Download the CSV or Excel version, or open the PDF, copy the stock names and paste them below.")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ImportError_("Old Excel (.xls) files aren't supported. Save it as .xlsx or CSV, or paste the stock names below.")
    return read_csv(data)


def read_text(text: str) -> List[List[str]]:
    rows = []
    for line in text.splitlines()[:MAX_ROWS]:
        cells = [c.strip() for c in re.split(r"[\t,;|]", line) if c.strip()]
        if cells:
            rows.append(cells)
    return rows


# ---------------- matching ----------------

def _match_cell(value: str):
    v = value.strip()
    if not v or re.fullmatch(r"[\d.,%\-+\s]+", v):
        return None
    sym = _SUFFIXES.sub("", re.sub(r"^(NSE|BSE)\s*[:\-]\s*", "", v, flags=re.I)).strip().upper()
    if sym in _BY_SYMBOL and _BY_SYMBOL[sym] is not None and re.fullmatch(r"[A-Z0-9&\-]{1,20}", sym):
        return _BY_SYMBOL[sym]
    return _BY_NAME.get(norm(v))


def find_holdings(rows: List[List[str]], loose: bool = False) -> Tuple[List[dict], List[str]]:
    """(catalog items found, in file order and without repeats; names that were not recognised)."""
    header, name_cols = -1, None
    if not loose:
        for i, row in enumerate(rows[:40]):
            if sum(1 for c in row if c) >= 3 and any(_HEADER_WORDS.search(c) and not re.search(r"isin", c, re.I) for c in row):
                header = i
                name_cols = [j for j, c in enumerate(row) if _HEADER_WORDS.search(c) and not re.search(r"isin", c, re.I)]
                break
    body = rows[header + 1:] if header >= 0 else rows
    found: Dict[str, dict] = {}
    unknown: List[str] = []
    for row in body:
        cols = name_cols if name_cols else (range(len(row)) if loose else range(min(3, len(row))))
        cells = [(j, row[j]) for j in cols if j < len(row) and row[j].strip()]
        if not cells:
            continue
        hit = next((m for _, v in cells if (m := _match_cell(v))), None)
        if hit:
            found.setdefault(hit["symbol"], hit)
            continue
        label = next((v for _, v in cells if not re.fullmatch(r"[\d.,%\-+\s]+", v)), "")
        if label and not _SKIP_ROWS.match(label.strip()) and len(label) <= 80 and label not in unknown:
            unknown.append(label)
    return list(found.values()), unknown[:60]
