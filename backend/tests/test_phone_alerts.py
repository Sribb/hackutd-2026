"""Text and voice alerts (#160) against a fake Twilio: auth, once per alert, masking, caps."""
from __future__ import annotations

import base64
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from backend.app import phone_alerts
from backend.app.main import app

OP = "op-token-for-tests"
H = {"X-Freeboard-Token": OP}


class FakeTwilio(BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        FakeTwilio.hits.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                "form": dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))})
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"sid": "SMfake"}')

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("FREEBOARD_TOKEN", OP)
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_API_BASE"):
        monkeypatch.delenv(k, raising=False)
    phone_alerts.reset()
    FakeTwilio.hits = []
    with TestClient(app) as c:
        yield c
    phone_alerts.reset()


@pytest.fixture()
def twilio(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTwilio)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("TWILIO_FROM", "+15125550000")
    monkeypatch.setenv("TWILIO_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    yield srv
    srv.shutdown()


def wait_hits(n, timeout=5.0):
    t0 = time.time()
    while len(FakeTwilio.hits) < n and time.time() - t0 < timeout:
        time.sleep(0.02)
    return FakeTwilio.hits


def test_normalize_and_mask():
    assert phone_alerts.normalize("(214) 555-0134") == "+12145550134"
    assert phone_alerts.normalize("+1 214.555.0134") == "+12145550134"
    for bad in ("123", "(114) 555-0134", "214 155 0134", None, 2145550134):
        assert phone_alerts.normalize(bad) is None
    assert phone_alerts.mask("+12145550134") == "+1 214 ***-0134"


def test_subscribe_and_alert_need_the_operator_token(client):
    assert client.post("/api/phone/subscribe", json={"phone": "2145550134"}).status_code in (401, 403)
    assert client.post("/api/phone/alert", json={"alert_id": "a", "text": "pump 3 failed"}).status_code in (401, 403)


def test_without_a_provider_it_says_so_and_sends_nothing(client):
    assert client.get("/api/phone/status").json()["provider"] == "none"
    r = client.post("/api/phone/subscribe", json={"phone": "(214) 555-0134"}, headers=H).json()
    assert r["masked"] == "+1 214 ***-0134" and r["provider"] == "none" and "phone" not in r
    a = client.post("/api/phone/alert", json={"alert_id": "EV1", "text": "pump 3 failed, tank 3 shuts down at 2:47 am"}, headers=H).json()
    assert a["provider"] == "none" and a["queued"] == 0 and "pager" in a["detail"]


def test_twilio_text_and_call_once_per_alert(client, twilio):
    client.post("/api/phone/subscribe", json={"phone": "214-555-0134"}, headers=H)
    text = "pump 3 failed. tank 3 reaches shutdown at 2:47 am. cheapest fix: 2 trucks, $664."
    a = client.post("/api/phone/alert", json={"alert_id": "EV1", "text": text}, headers=H).json()
    assert a["provider"] == "twilio" and a["queued"] == 1 and a["text"].startswith("gaia: ")
    hits = wait_hits(2)
    paths = sorted(h["path"] for h in hits)
    assert paths == ["/2010-04-01/Accounts/ACtest/Calls.json", "/2010-04-01/Accounts/ACtest/Messages.json"]
    sms = next(h for h in hits if h["path"].endswith("Messages.json"))
    assert sms["form"] == {"To": "+12145550134", "From": "+15125550000", "Body": a["text"]}
    assert sms["auth"] == "Basic " + base64.b64encode(b"ACtest:secret-token").decode()
    call = next(h for h in hits if h["path"].endswith("Calls.json"))
    assert call["form"]["Twiml"].startswith("<Response><Say") and "$664" in call["form"]["Twiml"]
    # the same alert again reaches nobody
    again = client.post("/api/phone/alert", json={"alert_id": "EV1", "text": text}, headers=H).json()
    assert again["queued"] == 0


def test_text_is_cleaned_and_capped(client, twilio):
    client.post("/api/phone/subscribe", json={"phone": "2145550134"}, headers=H)
    a = client.post("/api/phone/alert", json={"alert_id": "EV2", "text": "x\x00y\n" + "z" * 900, "call": False}, headers=H).json()
    assert a["text"].startswith("gaia: x y") and len(a["text"]) <= phone_alerts.MAX_TEXT
    assert [h["path"] for h in wait_hits(1)] == ["/2010-04-01/Accounts/ACtest/Messages.json"]
