"""Importing the stocks someone already owns, from a broker's holdings file or pasted names."""
import io
import zipfile

import pytest

from buydips import config, db, importer
from tests.test_web import client, csrf, sign_in  # noqa: F401  (client is a fixture)


def xlsx(rows, shared=True):
    """A minimal .xlsx built in memory (the same structure Excel writes)."""
    strings, sheet = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, val in enumerate(row):
            ref = "ABCDEFGH"[c] + str(r)
            if isinstance(val, (int, float)):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            elif shared:
                strings.append(val); cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{val}</t></is></c>')
        sheet.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet)}</sheetData></worksheet>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>{"".join(f"<si><t>{s}</t></si>" for s in strings)}</sst>')
    return buf.getvalue()


GROWW_LIKE = [["Holdings statement for Test User"], ["Client ID", "0000"], [],
              ["Stock Name", "ISIN", "Quantity", "Average buy price", "Closing price", "Value"],
              ["RELIANCE INDUSTRIES LTD", "INE002A01018", 10, 2400, 2900, 29000],
              ["TATA CONSULTANCY SERVICES LTD", "INE467B01029", 5, 3300, 4100, 20500],
              ["NIPPON INDIA ETF GOLD BEES", "INF204KB17I5", 100, 50, 103, 10300],
              ["SOME UNLISTED SMALLCAP LTD", "INE0000", 3, 10, 12, 36],
              ["Total", "", "", "", "", 59836]]


def symbols(found):
    return [i["symbol"] for i in found]


# ---------------- reading and matching ----------------

def test_excel_files_with_a_preamble_are_read_and_matched_by_company_name():
    for shared in (True, False):
        found, unknown = importer.find_holdings(importer.read_file("h.xlsx", xlsx(GROWW_LIKE, shared)))
        assert symbols(found) == ["RELIANCE.NS", "TCS.NS", "GOLDBEES.NS"]
        assert unknown == ["SOME UNLISTED SMALLCAP LTD"]                # reported, not silently lost; totals row ignored


def test_csv_with_broker_tickers_comma_semicolon_or_tab_and_odd_encodings():
    for delim in (",", ";", "\t"):
        text = delim.join(["Instrument", "Qty.", "Avg. cost", "LTP"]) + "\n" + "\n".join(
            delim.join(r) for r in (["RELIANCE", "10", "2400", "2900"], ["INFY-EQ", "5", "1500", "1600"],
                                    ["NSE:HDFCBANK", "1", "1", "1"], ["SILVERBEES", "2", "10", "12"], ["AAPL", "1", "1", "1"]))
        found, _ = importer.find_holdings(importer.read_csv(text.encode("utf-8-sig")))
        assert symbols(found) == ["RELIANCE.NS", "INFY.NS", "HDFCBANK.NS", "SILVERBEES.NS", "AAPL"], delim
    cp = "Stock Name,Qty,Avg\nTata Consultancy Services Ltd,1,1\nHDFC Bank Ltd.,1,1\n".encode("cp1252")
    assert symbols(importer.find_holdings(importer.read_csv(cp))[0]) == ["TCS.NS", "HDFCBANK.NS"]


def test_pasted_names_work_in_any_shape_and_repeats_are_collapsed():
    found, unknown = importer.find_holdings(importer.read_text("Reliance Industries\nTCS, 10 shares\nreliance industries ltd\nnot a stock\nApple Inc\n\n"), loose=True)
    assert symbols(found) == ["RELIANCE.NS", "TCS.NS", "AAPL"] and unknown == ["not a stock"]


def test_numbers_and_headers_are_never_mistaken_for_stocks():
    rows = [["Stock Name", "Quantity", "Price"], ["10", "20", "30"], ["V", "1", "1"], ["ALL", "2", "2"], ["TCS", "3", "3"]]
    found, unknown = importer.find_holdings(rows)
    assert symbols(found) == ["TCS.NS"]                         # "V" and "ALL" are short tickers, not holdings we cover
    assert not {"10", "20", "30"} & set(unknown) and set(unknown) == {"V", "ALL"}


def test_retired_indexes_and_unknown_items_are_not_matched():
    found, unknown = importer.find_holdings([["Instrument", "Qty", "x"], ["NIFTY 50", "1", "1"], ["^GSPC", "1", "1"], ["SENSEX", "1", "1"]])
    assert found == [] and len(unknown) == 3


def test_unreadable_files_get_a_plain_message():
    with pytest.raises(importer.ImportError_, match="PDF"):
        importer.read_file("h.pdf", b"%PDF-1.7 ...")
    with pytest.raises(importer.ImportError_, match="xls"):
        importer.read_file("h.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 50)
    with pytest.raises(importer.ImportError_, match="too large"):
        importer.read_file("h.csv", b"a" * (importer.MAX_BYTES + 1))
    with pytest.raises(importer.ImportError_):
        importer.read_file("h.xlsx", b"PK\x03\x04 not really a zip")


def test_hostile_spreadsheets_are_refused_not_expanded():
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:                          # entity-expansion ("billion laughs") attempt
        z.writestr("xl/worksheets/sheet1.xml", f'<!DOCTYPE x [<!ENTITY a "aaaa">]><worksheet {ns}><sheetData/></worksheet>')
    with pytest.raises(importer.ImportError_):
        importer.read_xlsx(buf.getvalue())
    big = io.BytesIO()
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:    # tiny file that inflates enormously
        z.writestr("xl/worksheets/sheet1.xml", "<a>" + "x" * (importer._MAX_XML + 10) + "</a>")
    assert len(big.getvalue()) < importer.MAX_BYTES
    with pytest.raises(importer.ImportError_):
        importer.read_xlsx(big.getvalue())


# ---------------- the screens ----------------

def upload(client, data, name="holdings.xlsx", lvl="3", pasted=""):
    form = {"csrf": csrf(client, "/import"), "lvl": lvl, "pasted": pasted}
    if data is not None:
        form["file"] = (io.BytesIO(data), name)
    return client.post("/import", data=form, content_type="multipart/form-data")


def test_the_import_flow_previews_then_switches_on_only_what_is_confirmed(client):
    sign_in(client)
    page = client.get("/import").get_data(as_text=True)
    assert "Import your stocks" in page and 'enctype="multipart/form-data"' in page and "never connect to your broker" in page
    r = upload(client, xlsx(GROWW_LIKE), lvl="5")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "We found <b>3</b> stocks" in html and "alerts at 5%" in html
    assert "Reliance Industries" in html and "Nippon India ETF Gold BeES" in html and "SOME UNLISTED SMALLCAP LTD" in html
    assert db.get_subscriptions(db.get_or_create_user("a@x.com")["id"]) == {}          # a preview changes nothing
    r = client.post("/import/confirm", data={"csrf": csrf(client, "/import"), "lvl": "5", "sym": ["RELIANCE.NS", "GOLDBEES.NS"]})
    assert r.status_code == 302 and "saved=1" in r.headers["Location"]
    assert db.get_subscriptions(db.get_or_create_user("a@x.com")["id"]) == {"RELIANCE.NS": 5.0, "GOLDBEES.NS": 5.0}


def test_importing_adds_to_a_watchlist_and_keeps_existing_levels(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    db.set_subscriptions(uid, {"RELIANCE.NS": 1.5, "^NSEI": 1.0})
    html = upload(client, xlsx(GROWW_LIKE)).get_data(as_text=True)
    assert "Already on your watchlist" in html and "Reliance Industries" in html
    client.post("/import/confirm", data={"csrf": csrf(client, "/import"), "lvl": "3", "sym": ["RELIANCE.NS", "TCS.NS"]})
    assert db.get_subscriptions(uid) == {"RELIANCE.NS": 1.5, "^NSEI": 1.0, "TCS.NS": 3.0}


def test_the_watchlist_limit_holds_for_imports(client):
    sign_in(client)
    uid = db.get_or_create_user("a@x.com")["id"]
    from buydips import catalog
    stocks = [i["symbol"] for i in catalog.ITEMS if i["type"] == "stock" and i["group"] == "India"]
    db.set_subscriptions(uid, {s: 3.0 for s in stocks[:23]})
    names = "\n".join(catalog.name_of(s) for s in stocks[23:30])                      # 7 new, room for 2
    html = upload(client, None, pasted=names).get_data(as_text=True)
    assert "We ticked the first 2" in html and html.count("checked") >= 2
    over = client.post("/import/confirm", data={"csrf": csrf(client, "/import"), "sym": stocks[23:30]})
    assert over.status_code == 400 and len(db.get_subscriptions(uid)) == 23           # refused whole, nothing half-saved


def test_confirm_ignores_hostile_symbols_and_bad_levels(client):
    sign_in(client)
    client.post("/import/confirm", data={"csrf": csrf(client, "/import"), "lvl": "999", "sym": ["^GSPC", "NOPE", "<script>", "^NSEI", "TCS.NS"]})
    assert db.get_subscriptions(db.get_or_create_user("a@x.com")["id"]) == {"TCS.NS": 3.0}   # indexes/retired/unknown skipped; level fell back to 3


def test_empty_wrong_and_huge_submissions_get_clear_answers(client):
    sign_in(client)
    assert upload(client, None).status_code == 400
    assert "PDF" in upload(client, b"%PDF-1.4 xx", "h.pdf").get_data(as_text=True)
    assert "couldn't find any stocks" in upload(client, b"Name,Qty,P\nFoo Bar,1,1\n", "h.csv").get_data(as_text=True)
    assert "too large" in upload(client, b"a" * (importer.MAX_BYTES + 10), "h.csv").get_data(as_text=True)
    assert upload(client, b"a" * (importer.MAX_BYTES + 400_000), "h.csv").status_code == 413          # refused before it is read
    assert client.post("/watchlist", data={"csrf": "x", "pad": "a" * 70_000}).status_code == 413      # other pages keep the small limit


def test_import_needs_sign_in_and_a_valid_csrf_token(client):
    anon = client.application.test_client()
    assert anon.get("/import").status_code == 302 and anon.post("/import/confirm", data={"sym": "TCS.NS"}).status_code == 302
    sign_in(client)
    assert client.post("/import/confirm", data={"sym": ["TCS.NS"]}).status_code in (400, 403)
    assert client.post("/import", data={"pasted": "TCS"}).status_code in (400, 403)
    assert db.get_subscriptions(db.get_or_create_user("a@x.com")["id"]) == {}


def test_the_file_and_its_numbers_are_never_stored(client):
    sign_in(client)
    upload(client, xlsx(GROWW_LIKE))
    from sqlalchemy import text
    with db.engine().connect() as c:
        dump = " ".join(str(v) for t in ("users", "subscriptions", "buckets", "bucket_items", "alerts_sent")
                        for row in c.execute(text(f"select * from {t}")) for v in row)
    assert "INE002A01018" not in dump and "29000" not in dump and "Test User" not in dump


def test_entry_points_and_wording(client):
    anon = client.application.test_client()
    home = anon.get("/").get_data(as_text=True)
    assert "Can I import the stocks I already own?" in home and "holdings file" in home
    assert "never connect to your broker" in anon.get("/privacy").get_data(as_text=True)
    sign_in(client)
    first = client.get("/watchlist").get_data(as_text=True)
    assert 'href="/import"' in first and "Import my holdings" in first
    assert 'href="/import"' in client.get("/watchlist?start=india").get_data(as_text=True)
    assert "/static/importfile.js" in client.get("/import").get_data(as_text=True)
