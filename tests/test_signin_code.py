"""Signing in with the emailed 6-digit code, and links that are used up by the Continue button."""
import re
from datetime import timedelta

import pytest

from buydips import config, db, emails
from tests.test_web import client, csrf  # noqa: F401  (client is a fixture)


def request_link(client, email="a@x.com"):
    client.post("/login", data={"email": email, "csrf": csrf(client)})
    body = client.sent[-1][2]
    pin = re.search(r"code on the sign-in page: (\d{3}) (\d{3})", body)
    return re.search(r"/signin/([a-z2-7]{16})", body).group(1), pin.group(1) + pin.group(2)


def enter(client, email, pin):
    return client.post("/login/code", data={"email": email, "pin": pin, "csrf": csrf(client)})


def signed_in(client):
    return client.get("/watchlist?start=india").status_code == 200


def test_the_email_carries_a_link_and_a_six_digit_code(client):
    code, pin = request_link(client)
    assert len(pin) == 6 and pin.isdigit()
    _, text, html = emails.signin("https://x/signin/abc", 15, "482913")
    assert "482 913" in text and "482 913" in html and "another device" in html.lower()
    plain = emails.signin("https://x/signin/abc", 15)
    assert "482" not in plain[1] and "another device" not in plain[2].lower()      # no code, no mention of one


def test_the_right_code_signs_you_in_once(client):
    _, pin = request_link(client)
    r = enter(client, "a@x.com", pin)
    assert r.status_code == 302 and signed_in(client)
    other = client.application.test_client()
    assert other.post("/login/code", data={"email": "a@x.com", "pin": pin, "csrf": csrf(other)}).status_code == 400   # used up


def test_spaces_in_the_code_are_fine_and_email_case_does_not_matter(client):
    _, pin = request_link(client, "Mixed@X.com")
    assert enter(client, "MIXED@x.com", f"{pin[:3]} {pin[3:]}").status_code == 302


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 5", "１２３４５６", "123456\n7", "1" * 500])
def test_malformed_codes_fail_without_using_a_try(client, bad):
    request_link(client)
    assert enter(client, "a@x.com", bad).status_code == 400
    with db.engine().connect() as c:
        from sqlalchemy import select
        assert c.execute(select(db.signin_pins.c.tries)).scalar() == 0


@pytest.mark.parametrize("fmt", ["{a}{b}", "{a} {b}", " {a} {b} ", "{a}-{b}", "{a}\u00a0{b}", "{a}\u200b{b}", "\t{a}{b}\n"])
def test_a_code_copied_with_odd_spacing_still_works(client, fmt):
    _, pin = request_link(client)
    assert enter(client, "a@x.com", fmt.format(a=pin[:3], b=pin[3:])).status_code == 302


def test_a_code_from_an_earlier_email_still_works_after_asking_again(client):
    _, first = request_link(client)
    _, second = request_link_after_cooldown(client)
    _, third = request_link_after_cooldown(client)
    assert enter(client, "a@x.com", first).status_code == 302


def test_only_the_newest_few_codes_are_kept(client):
    pins = [db.create_login_pin("a@x.com", 15) for _ in range(db.PINS_KEPT + 2)]
    with db.engine().connect() as c:
        from sqlalchemy import func, select
        assert c.execute(select(func.count()).select_from(db.signin_pins)).scalar() == db.PINS_KEPT
    assert db.check_login_pin("a@x.com", pins[-1], 5) is True


def test_the_failure_reason_is_logged_without_the_code_or_the_address(client, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="buyDips.web")
    _, pin = request_link(client, "secret.person@x.com")
    wrong = "000000" if pin != "000000" else "111111"
    enter(client, "secret.person@x.com", wrong)
    enter(client, "nobody@x.com", "123456")
    enter(client, "secret.person@x.com", "12")
    text = caplog.text
    assert "wrong" in text and "none" in text and "malformed" in text
    assert wrong not in text and "secret.person" not in text and pin not in text


def test_deleting_an_account_removes_waiting_codes_and_summaries(client):
    uid = db.get_or_create_user("a@x.com")["id"]
    db.create_login_pin("a@x.com", 15)
    db.create_login_code("a@x.com", 15)
    db.save_summary(uid, "abc", "Two items are up.")
    db.delete_user(uid)
    from sqlalchemy import func, select
    with db.engine().connect() as c:
        for table in (db.signin_pins, db.login_codes, db.summaries):
            assert c.execute(select(func.count()).select_from(table)).scalar() == 0


def test_a_wrong_code_fails_and_the_real_one_still_works_after(client):
    _, pin = request_link(client)
    wrong = "000000" if pin != "000000" else "111111"
    r = enter(client, "a@x.com", wrong)
    body = r.get_data(as_text=True)
    assert r.status_code == 400 and "That code didn't work" in body and not signed_in(client)
    assert enter(client, "a@x.com", pin).status_code == 302


def test_five_wrong_tries_cancel_the_code_even_for_the_right_one(client):
    _, pin = request_link(client)
    wrong = "000000" if pin != "000000" else "111111"
    for _ in range(config.LOGIN_PIN_TRIES):
        assert enter(client, "a@x.com", wrong).status_code == 400
    assert enter(client, "a@x.com", pin).status_code == 400 and not signed_in(client)
    new_code, new_pin = request_link_after_cooldown(client)
    assert enter(client, "a@x.com", new_pin).status_code == 302


def request_link_after_cooldown(client):
    with db.engine().begin() as conn:
        from sqlalchemy import update
        conn.execute(update(db.users).values(last_login_sent_at=db._now() - timedelta(minutes=5)))
    return request_link(client)


def test_a_code_belongs_to_one_email_only(client):
    _, pin = request_link(client, "a@x.com")
    assert enter(client, "b@x.com", pin).status_code == 400
    assert enter(client, "a@x.com", pin).status_code == 302          # b's attempts did not touch a's tries


def test_an_expired_code_fails(client):
    pin = db.create_login_pin("a@x.com", -1)
    assert enter(client, "a@x.com", pin).status_code == 400


def test_unknown_emails_get_the_same_failure_and_no_account_is_made(client):
    r = enter(client, "nobody@x.com", "123456")
    assert r.status_code == 400 and "That code didn't work" in r.get_data(as_text=True)
    with db.engine().connect() as c:
        from sqlalchemy import select
        assert c.execute(select(db.users.c.id).where(db.users.c.email == "nobody@x.com")).first() is None


def test_codes_are_not_stored_in_clear_and_each_works_once(client):
    first = db.create_login_pin("a@x.com", 15)
    second = db.create_login_pin("a@x.com", 15)
    with db.engine().connect() as c:
        from sqlalchemy import select
        rows = c.execute(select(db.signin_pins.c.pin_hash, db.signin_pins.c.email)).all()
    assert len(rows) == (2 if first != second else 1) and all(first not in r[0] and second not in r[0] for r in rows)
    assert db.check_login_pin("a@x.com", second, 5) is True
    assert db.check_login_pin("a@x.com", second, 5) is False        # and it works once


def test_the_login_code_form_needs_csrf(client):
    _, pin = request_link(client)
    assert client.post("/login/code", data={"email": "a@x.com", "pin": pin}).status_code == 400


def test_the_inbox_page_shows_the_code_box_and_does_not_leak_whether_the_email_exists(client):
    html = client.post("/login", data={"email": "a@x.com", "csrf": csrf(client)}).get_data(as_text=True)
    assert 'name="pin"' in html and 'autocomplete="one-time-code"' in html and 'action="/login/code"' in html
    again = client.post("/login", data={"email": "a@x.com", "csrf": csrf(client)}).get_data(as_text=True)    # inside the cooldown
    assert 'name="pin"' in again and len(client.sent) == 1


# ---------------- links ----------------

def test_opening_the_link_does_not_sign_in_or_use_it_up(client):
    code, _ = request_link(client)
    scanner = client.application.test_client()
    for _ in range(3):                                     # an email scanner pre-opening the link
        r = scanner.get(f"/signin/{code}")
        assert r.status_code == 200 and "Continue to buyDips" in r.get_data(as_text=True)
    assert not signed_in(scanner)
    assert client.post(f"/signin/{code}", data={"csrf": csrf(client, f"/signin/{code}")}).status_code == 302
    assert signed_in(client)


def test_a_link_works_once(client):
    code, _ = request_link(client)
    assert client.post(f"/signin/{code}", data={"csrf": csrf(client, f"/signin/{code}")}).status_code == 302
    other = client.application.test_client()
    page = other.get(f"/signin/{code}")
    assert page.status_code == 400 and not signed_in(other)


def test_the_continue_button_needs_csrf(client):
    code, _ = request_link(client)
    assert client.post(f"/signin/{code}", data={}).status_code == 400
    assert client.get(f"/signin/{code}").status_code == 200       # the link is still there to use


def test_signing_in_one_way_cancels_the_other(client):
    code, pin = request_link(client)
    assert enter(client, "a@x.com", pin).status_code == 302
    other = client.application.test_client()
    assert other.get(f"/signin/{code}").status_code == 400
    second = client.application.test_client()                      # a fresh browser asks again, after the cooldown
    second.sent = client.sent
    with db.engine().begin() as conn:
        from sqlalchemy import update
        conn.execute(update(db.users).values(last_login_sent_at=db._now() - timedelta(minutes=5)))
    code2, pin2 = request_link(second)
    assert second.post(f"/signin/{code2}", data={"csrf": csrf(second, f"/signin/{code2}")}).status_code == 302
    assert not db.check_login_pin("a@x.com", pin2, 5)               # signing in by link cancelled the code
