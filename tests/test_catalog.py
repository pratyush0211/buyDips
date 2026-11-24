from buydips import catalog


def test_catalog_is_well_formed():
    symbols = [i["symbol"] for i in catalog.ITEMS]
    assert len(symbols) == len(set(symbols)), "duplicate symbols"
    for i in catalog.ITEMS:
        assert i["name"].strip() and i["type"] in ("index", "stock", "etf")
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
    assert titles[-3:] == ["Indian stocks (NSE)", "US stocks (Nasdaq-100)", "Gold & silver ETFs (NSE)"]
    assert titles[0] == "Indexes: India"


def test_both_alphabet_share_classes_are_distinguishable():
    assert catalog.name_of("GOOGL") != catalog.name_of("GOOG")


def test_search_text_includes_everyday_names():
    by = {s["symbol"]: s for sec in catalog.grouped() for s in sec["items"]}
    assert "google" in by["GOOGL"]["search"] and "facebook" in by["META"]["search"]
    assert "zomato" in by["ETERNAL.NS"]["search"] and "reliance" in by["RELIANCE.NS"]["search"]


def test_every_alias_points_at_a_real_symbol():
    assert set(catalog.ALIASES) <= set(catalog.BY_SYMBOL)


def test_currency_symbols():
    assert catalog.currency_symbol("RELIANCE.NS") == "₹"
    assert catalog.currency_symbol("AAPL") == "$"
    assert catalog.currency_symbol("^NSEI") == "" and catalog.currency_symbol("nonsense") == ""


def test_tab_totals_cover_every_item_once():
    totals = catalog.tab_totals()
    assert sum(totals.values()) == len(catalog.ITEMS)
    assert totals["indexes"] == 8 and totals["india"] >= 450 and totals["us"] >= 95 and totals["metals"] == 2
    assert {s["tab"] for s in catalog.grouped()} == {"indexes", "india", "us", "metals"}


def test_only_india_and_us_are_supported():
    assert {i["group"] for i in catalog.ITEMS} == {"India", "US"}
    for gone in ("^FTSE", "^GDAXI", "^N225", "^HSI"):
        assert gone not in catalog.BY_SYMBOL


def test_gold_and_silver_etfs():
    for sym, word in (("GOLDBEES.NS", "gold"), ("SILVERBEES.NS", "silver")):
        item = catalog.BY_SYMBOL[sym]
        assert item["type"] == "etf" and item["group"] == "India"
        assert word in catalog.search_text(item)
        assert catalog.currency_symbol(sym) == "₹" and catalog.default_threshold(sym) == 2.0
    from buydips import markets
    assert markets.market_of("GOLDBEES.NS") == "India"      # so Indian hours and holidays apply
    metals = [s for s in catalog.grouped() if s["tab"] == "metals"]
    assert len(metals) == 1 and [i["symbol"] for i in metals[0]["items"]] == ["GOLDBEES.NS", "SILVERBEES.NS"] \
        or {i["symbol"] for i in metals[0]["items"]} == {"GOLDBEES.NS", "SILVERBEES.NS"}
