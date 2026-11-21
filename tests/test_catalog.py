from buydips import catalog


def test_catalog_is_well_formed():
    symbols = [i["symbol"] for i in catalog.ITEMS]
    assert len(symbols) == len(set(symbols)), "duplicate symbols"
    for i in catalog.ITEMS:
        assert i["name"].strip() and i["type"] in ("index", "stock")
        if i["type"] == "index":
            assert i["symbol"].startswith("^")
        elif i["group"] == "India":
            assert i["symbol"].endswith(".NS")
        else:
            assert i["group"] == "US" and i["symbol"].isalpha() and i["symbol"].isupper()


def test_catalog_covers_the_nifty_500_and_the_main_indexes():
    india = [i for i in catalog.ITEMS if i["type"] == "stock" and i["group"] == "India"]
    us = [i for i in catalog.ITEMS if i["type"] == "stock" and i["group"] == "US"]
    assert len(india) >= 450 and len(us) >= 95
    for must_have in ("^NSEI", "^BSESN", "^GSPC", "RELIANCE.NS", "TCS.NS", "ETERNAL.NS", "AAPL", "NVDA", "MSFT"):
        assert must_have in catalog.BY_SYMBOL


def test_stocks_are_listed_alphabetically_within_each_market():
    for group in ("India", "US"):
        names = [i["name"].lower() for i in catalog.ITEMS if i["type"] == "stock" and i["group"] == group]
        assert names == sorted(names)


def test_watchlist_sections_put_indian_stocks_before_us_stocks():
    titles = [s["title"] for s in catalog.grouped()]
    assert titles[-2:] == ["Indian stocks (NSE)", "US stocks (Nasdaq-100)"]
    assert titles[0] == "Indexes: India"


def test_both_alphabet_share_classes_are_distinguishable():
    assert catalog.name_of("GOOGL") != catalog.name_of("GOOG")
