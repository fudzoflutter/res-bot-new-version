"""Admin Mini App URL portability checks."""
import pytest
from app.utils.webapp_url import webapp_url


def test_missing_scheme_is_normalized(monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "solution-team-robot.up.railway.app")
    assert webapp_url() == "https://solution-team-robot.up.railway.app/"


def test_current_railway_domain_is_fallback(monkeypatch):
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "new-account.up.railway.app")
    assert webapp_url() == "https://new-account.up.railway.app/"


def test_explicit_custom_domain_has_priority(monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "https://panel.example.com/admin")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "new-account.up.railway.app")
    assert webapp_url() == "https://panel.example.com/admin"


@pytest.mark.parametrize("invalid", ["http://bad.example", "ftp://example.com", "https:///missing-host", "https://bad host"])
def test_invalid_urls_raise(monkeypatch, invalid):
    monkeypatch.setenv("WEBAPP_URL", invalid)
    with pytest.raises(ValueError):
        webapp_url()


def test_missing_configuration_explains_setup(monkeypatch):
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    with pytest.raises(RuntimeError, match="Generate Domain"):
        webapp_url()
