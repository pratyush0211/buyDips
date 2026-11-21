from buydips import emails


def item(name="Nifty 50", pct=-2.0):
    return {"name": name, "change_pct": pct, "price": 98.0, "prev_close": 100.0, "session": "2026-11-20", "threshold": 1.0}


def test_signin_email_has_button_and_plain_text_fallback():
    url = "https://buydips.in/signin/abcdefghijklmnop"
    subject, text, html = emails.signin(url, 15)
    assert subject == "Your buyDips sign-in link"
    assert url in text and "15 minutes" in text
    assert f'href="{url}"' in html and "Sign in to buyDips" in html


def test_alert_email_html_and_text_agree():
    subject, text, html = emails.alert([item(), item("S&P 500", -3.0)], "https://buydips.in/unsubscribe/x")
    assert subject == "buyDips: 2 of your watchlist are down"
    for part in (text, html):
        assert "S&amp;P 500" in part or "S&P 500" in part
    assert 'href="https://buydips.in/unsubscribe/x"' in html and "Unsubscribe: https://buydips.in/unsubscribe/x" in text
    assert "not investment advice" in html.lower() and "not investment advice" in text.lower()


def test_html_is_escaped():
    _, _, html = emails.alert([item("<script>alert(1)</script>")], "https://buydips.in/u")
    assert "<script>" not in html and "&lt;script&gt;" in html
