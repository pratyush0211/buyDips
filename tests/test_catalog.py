from buydips import catalog


def test_catalog_is_well_formed():
    symbols = [i["symbol"] for i in catalog.ITEMS]
    assert len(symbols) == len(set(symbols)), "duplicate symbols"
    for i in catalog.ITEMS:
        assert i["name"].strip() and i["type"] in ("index", "stock")
        assert i["symbol"].startswith("^") if i["type"] == "index" else i["symbol"].endswith(".NS")


def test_catalog_covers_the_nifty_500_and_the_main_indexes():
    stocks = [i for i in catalog.ITEMS if i["type"] == "stock"]
    assert len(stocks) >= 450
    for must_have in ("^NSEI", "^BSESN", "^GSPC", "RELIANCE.NS", "TCS.NS", "ETERNAL.NS"):
        assert must_have in catalog.BY_SYMBOL


def test_stocks_are_listed_alphabetically_for_browsing():
    names = [i["name"].lower() for i in catalog.ITEMS if i["type"] == "stock"]
    assert names == sorted(names)
