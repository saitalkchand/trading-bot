"""The control panel only answers itself: other web pages in the browser can't drive the bot."""
import pytest


@pytest.fixture(autouse=True)
def workdir(monkeypatch):
    """The panel reads config.py/settings.json from its own folder; these tests only read or get refused."""
    from conftest import ROOT
    monkeypatch.chdir(ROOT)


@pytest.fixture(scope="module")
def client():
    import app
    app.app.testing = True
    return app.app.test_client()


BASE = "http://127.0.0.1:8765"


def test_state_readable(client):
    assert client.get("/api/state", base_url=BASE).status_code == 200


def test_foreign_host_refused(client):
    # DNS rebinding: evil.com resolving to 127.0.0.1
    assert client.get("/api/state", base_url="http://evil.example:8765").status_code == 403


def test_foreign_origin_refused(client):
    r = client.post("/api/mode", json={"mode": "live"}, base_url=BASE, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_text_plain_post_refused(client):
    # a <form>/no-cors fetch can send text/plain without a CORS preflight
    r = client.post("/api/bot/start", data='{"confirm":"YES"}', content_type="text/plain", base_url=BASE)
    assert r.status_code == 415


def test_own_json_post_passes_the_guard(client):
    r = client.post("/api/mode", json={"mode": "bogus"}, base_url=BASE, headers={"Origin": BASE})
    assert r.status_code in (400, 409)   # reached the handler; invalid mode, nothing written
