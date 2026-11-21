import pytest

from buydips import config, mailer


class Resp:
    def __init__(self, ok=True, status=200, text=""):
        self.ok, self.status_code, self.text = ok, status, text


@pytest.fixture()
def calls(monkeypatch):
    calls = []
    monkeypatch.setattr(mailer.requests, "post", lambda url, **kw: calls.append((url, kw)) or Resp())
    return calls


def use(monkeypatch, provider):
    monkeypatch.setattr(config, "EMAIL_PROVIDER", provider)
    monkeypatch.setattr(config, "EMAIL_API_KEY", "key123")
    monkeypatch.setattr(config, "MAIL_FROM", "alerts@example.com")


def test_brevo_payload(monkeypatch, calls):
    use(monkeypatch, "brevo")
    assert mailer.send_email("u@x.com", "Hi", "Body", unsubscribe_url="https://s/u/1")
    url, kw = calls[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    assert kw["headers"]["api-key"] == "key123"
    assert kw["json"]["to"] == [{"email": "u@x.com"}]
    assert kw["json"]["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_resend_payload(monkeypatch, calls):
    use(monkeypatch, "resend")
    assert mailer.send_email("u@x.com", "Hi", "Body", unsubscribe_url="https://s/u/1")
    url, kw = calls[0]
    assert url == "https://api.resend.com/emails"
    assert kw["headers"]["Authorization"] == "Bearer key123"
    assert kw["json"]["text"] == "Body"


def test_api_error_returns_false(monkeypatch):
    use(monkeypatch, "brevo")
    monkeypatch.setattr(mailer.requests, "post", lambda *a, **k: Resp(ok=False, status=401, text="bad key"))
    assert mailer.send_email("u@x.com", "Hi", "Body") is False


def test_unconfigured_sends_nothing(monkeypatch, calls):
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(config, "SMTP_USER", "")
    assert mailer.send_email("u@x.com", "Hi", "Body") is False
    assert calls == []
