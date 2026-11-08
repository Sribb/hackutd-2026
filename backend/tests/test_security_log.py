"""Security event log (#126).

Each test builds a small app with auth.require_role routes, origin_guard.install, then
security_log.install, the order main.py uses. The log must note wrong tokens, cross-site
writes, role refusals, lockouts, rejected uploads, and refused /ws connections, fold
repeats into one event, never change a response, and never keep a query string, a
header, a token, a PIN, or a full address."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import auth, origin_guard, security_log
from backend.app.audit import AuditLog
from backend.app.hub import Client, Hub
from backend.app.security_log import SecurityLog

PIN, PHONE = "4821", "phone-secret-9"
EVIL = {"Origin": "https://evil.example"}
KEYS = {"id", "kind", "method", "path", "client", "first_t", "last_t", "count", "detail"}


@pytest.fixture(autouse=True)
def tokens_on(monkeypatch):
    for var in ("FREEBOARD_TOKEN", "FREEBOARD_ALLOWED_HOSTS", "FREEBOARD_AUDIT_FILE",
                "FREEBOARD_SECURITY_DEDUPE_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPERATOR_PIN", PIN)
    monkeypatch.setenv("FREEBOARD_PHONE_TOKEN", PHONE)


class FakeClock:
    """A monotonic clock the test moves by hand, so no test sleeps through a window."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeSocket:
    """Stands in for a console's websocket inside the real Hub."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def make_app(seclog=None, live=None, with_log=True):
    app = FastAPI()
    operator = Depends(auth.require_role(auth.OPERATOR))

    @app.post("/api/op")
    def op(caller: auth.Caller = operator) -> dict:
        return {"role": caller.role}

    @app.post("/api/op/{n}")
    def op_n(n: str, caller: auth.Caller = operator) -> dict:
        return {"n": n}

    @app.post("/api/busy")   # stands in for #57's PIN lockout
    def busy() -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": "Too many tries. Wait a minute."})

    @app.post("/api/upload/big")
    def big() -> dict:
        raise HTTPException(413, "Uploads must be under 20 MB.")

    @app.post("/api/upload/odd")
    def odd() -> dict:
        raise HTTPException(415, "Send the blueprint as application/json.")

    @app.post("/other")
    def other(caller: auth.Caller = operator) -> dict:
        return {}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token: str = "") -> None:   # live_api's rule
        ok, _caller = auth.ws_caller(token)
        await websocket.accept()
        if not ok:
            await websocket.close(code=1008, reason=auth.DENIED)
            return
        await websocket.send_json({"type": "state"})
        await websocket.close()

    app.state.audit = AuditLog()
    if live is not None:
        app.state.live = live
    origin_guard.install(app)
    return app, (security_log.install(app, seclog) if with_log else None)


def h(token: str) -> dict:
    return {"X-Freeboard-Token": token}


def security_audit(app) -> list[dict]:
    return [e for e in app.state.audit.entries() if e["event"] == "security"]


def wait_for(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


# ---------- what gets noted ----------

def test_a_wrong_token_is_one_auth_failed_event():
    app, seclog = make_app()
    c = TestClient(app)
    assert c.post("/api/op", headers=h("nope")).status_code == 401
    assert c.post("/api/op", headers=h(PIN)).status_code == 200   # the right PIN is not an event
    events = c.get("/api/security/events").json()["events"]
    assert len(events) == 1
    e = events[0]
    assert set(e) == KEYS
    assert (e["kind"], e["method"], e["path"], e["count"]) == ("auth_failed", "POST", "/api/op", 1)
    assert e["detail"] == "401: missing or wrong token."
    assert e["first_t"] == e["last_t"] and e["first_t"].endswith("+00:00")


def test_a_repeat_inside_the_window_counts_and_is_audited_once():
    clock = FakeClock()
    app, seclog = make_app(SecurityLog(clock=clock))
    c = TestClient(app)
    c.post("/api/op", headers=h("nope"))
    clock.t += 59
    c.post("/api/op")   # no token at all: the same kind, path, and client
    [e] = seclog.events()
    assert e["count"] == 2
    [a] = security_audit(app)
    assert (a["source"], a["kind"], a["path"], a["client"]) == ("security", "auth_failed", "/api/op", e["client"])


def test_after_the_window_it_is_a_new_event(monkeypatch):
    clock = FakeClock()
    app, seclog = make_app(SecurityLog(clock=clock))
    c = TestClient(app)
    c.post("/api/op")
    clock.t += 30
    c.post("/api/op")
    clock.t += 31   # 61 s after the first: the default window (60 s) is over
    c.post("/api/op")
    events = seclog.events()
    assert [e["count"] for e in events] == [2, 1] and events[1]["id"] > events[0]["id"]
    assert len(security_audit(app)) == 2
    monkeypatch.setenv("FREEBOARD_SECURITY_DEDUPE_S", "5")
    clock.t += 6
    c.post("/api/op")
    assert [e["count"] for e in seclog.events()] == [2, 1, 1]


def test_a_cross_site_post_is_cross_site():
    app, seclog = make_app()
    c = TestClient(app)
    r = c.post("/api/op", headers={**EVIL, **h(PIN)})
    assert r.status_code == 403 and r.json() == {"detail": origin_guard.REFUSED}
    [e] = seclog.events()
    assert (e["kind"], e["path"]) == ("cross_site", "/api/op")
    assert e["detail"] == "403: a write from a page on another site was refused."


def test_the_phone_token_on_an_operator_route_is_forbidden_role():
    app, seclog = make_app()
    c = TestClient(app)
    r = c.post("/api/op", headers=h(PHONE))
    assert r.status_code == 403 and "operator PIN" in r.json()["detail"]
    [e] = seclog.events()
    assert e["kind"] == "forbidden_role" and e["detail"] == "403: this token isn't allowed to do that."


def test_a_429_is_a_lockout():
    app, seclog = make_app()
    c = TestClient(app)
    assert c.post("/api/busy").status_code == 429
    [e] = seclog.events()
    assert (e["kind"], e["path"], e["detail"]) == ("lockout", "/api/busy", "429: too many requests, refused for now.")


def test_uploads_too_large_or_of_the_wrong_type_are_upload_rejected():
    app, seclog = make_app()
    c = TestClient(app)
    assert c.post("/api/upload/big").status_code == 413
    assert c.post("/api/upload/odd").status_code == 415
    assert [(e["kind"], e["detail"]) for e in seclog.events()] == [
        ("upload_rejected", "413: the upload is too large."),
        ("upload_rejected", "415: the upload's type isn't accepted.")]


def test_a_wrong_ws_token_is_ws_auth_failed():
    app, seclog = make_app()
    c = TestClient(app)
    with c.websocket_connect("/ws?token=nope") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008
    for fine in ("/ws", f"/ws?token={PHONE}"):   # read-only, and a good token, are not events
        with c.websocket_connect(fine) as ws:
            assert ws.receive_json() == {"type": "state"}
    [e] = seclog.events()
    assert (e["kind"], e["method"], e["path"]) == ("ws_auth_failed", "WS", "/ws")


def test_only_api_and_ws_are_watched_and_the_read_is_open():
    app, seclog = make_app()
    c = TestClient(app)
    assert c.post("/other").status_code == 401   # outside /api/: not noted
    assert seclog.events() == []
    c.post("/api/op")
    c.post("/api/busy")
    r = c.get("/api/security/events")   # tokens are on, and the read needs none
    assert r.status_code == 200
    first, second = r.json()["events"]
    assert (first["kind"], second["kind"]) == ("auth_failed", "lockout")
    assert c.get(f"/api/security/events?since={first['id']}").json() == {"events": [second]}


def test_the_first_occurrence_reaches_every_screen():
    hub, sock = Hub(), FakeSocket()
    hub.clients["c1"] = Client(id="c1", ws=sock)
    app, seclog = make_app(live=SimpleNamespace(hub=hub))
    with TestClient(app) as c:
        c.post("/api/op")
        c.post("/api/op")   # a repeat: counted, not sent again
        assert wait_for(lambda: len(sock.sent) >= 1)
        time.sleep(0.05)
    [msg] = sock.sent
    assert msg["type"] == "security" and msg["event"]["kind"] == "auth_failed"
    assert set(msg["event"]) == KEYS and msg["event"]["count"] == 1
    assert seclog.events()[0]["count"] == 2


# ---------- what is never kept or changed ----------

def test_a_token_in_the_query_never_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG, logger="freeboard.security")
    hub, sock = Hub(), FakeSocket()
    hub.clients["c1"] = Client(id="c1", ws=sock)
    app, seclog = make_app(live=SimpleNamespace(hub=hub))
    with TestClient(app) as c:
        assert c.post("/api/op?token=abc").status_code == 401
        assert c.post("/api/op?token=abc", headers=h("abc")).status_code == 401
        assert c.post("/api/op%3Ftoken=abc", headers=EVIL).status_code == 403   # %3F decodes to "?"
        with c.websocket_connect("/ws?role=web&token=abc") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert wait_for(lambda: len(sock.sent) == 3)
    assert sorted((e["kind"], e["path"]) for e in seclog.events()) == [
        ("auth_failed", "/api/op"), ("cross_site", "/api/op"), ("ws_auth_failed", "/ws")]
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("freeboard")]
    assert ours   # the log lines were captured, and are checked below
    everything = json.dumps([seclog.events(), app.state.audit.entries(), sock.sent, ours])
    assert "abc" not in everything and "token=" not in everything


def test_clients_are_masked():
    m = security_log.mask_client
    assert m(("203.0.113.7", 5000)) == "203.0.113.x"
    assert m(["198.51.100.23", 1]) == "198.51.100.x"
    assert m(("::ffff:198.51.100.23", 1)) == "198.51.100.x"
    assert m(("2001:db8:85a3:8d3:1319:8a2e:370:7348", 1)) == "2001:db8:85a3::/48"
    assert m(("fe80::1%eth0", 1)) == "fe80::/48"
    for junk in (None, ("testclient", 50000), ("", 0), [], "not an ip"):
        assert m(junk) == "unknown"

    app, seclog = make_app()

    async def from_address(scope, receive, send):   # scope["client"] as uvicorn fills it
        await app({**scope, "client": ("203.0.113.7", 5555)}, receive, send)

    assert TestClient(from_address).post("/api/op").status_code == 401
    [e] = seclog.events()
    assert e["client"] == "203.0.113.x"
    assert "203.0.113.7" not in json.dumps([seclog.events(), app.state.audit.entries()])


def test_paths_and_the_window_setting_are_cleaned(monkeypatch):
    p = security_log.clean_path
    assert p("/api/op?token=x") == "/api/op"
    assert p("/api/op#k=x") == "/api/op"
    assert p("/api/a\nb\x00") == "/api/a_b_"
    assert p("/api/" + "x" * 500) == "/api/" + "x" * 195 + "..."
    assert p(None) == "/"
    for raw, want in (("", 60.0), ("nope", 60.0), ("-1", 60.0), ("nan", 60.0), ("inf", 60.0), ("0", 0.0),
                      ("2.5", 2.5)):
        monkeypatch.setenv("FREEBOARD_SECURITY_DEDUPE_S", raw)
        assert security_log.dedupe_window() == want, raw


def test_responses_are_unchanged():
    watched, _ = make_app()
    plain, _ = make_app(with_log=False)
    a, b = TestClient(watched), TestClient(plain)
    for method, path, headers in (("post", "/api/op", {}), ("post", "/api/op", h(PHONE)),
                                  ("post", "/api/op", EVIL), ("post", "/api/busy", {}),
                                  ("post", "/api/upload/big", {}), ("post", "/api/upload/odd", {}),
                                  ("post", "/api/op", h(PIN)), ("get", "/api/missing", {})):
        ra, rb = (getattr(x, method)(path, headers=headers) for x in (a, b))
        assert (ra.status_code, ra.content, ra.headers.multi_items()) == \
               (rb.status_code, rb.content, rb.headers.multi_items()), (method, path)


def test_hub_and_audit_failures_never_break_a_request():
    class Broken:
        def append(self, *args, **kwargs):
            raise OSError("disk full")

        def broadcast(self, *args, **kwargs):
            raise RuntimeError("hub down")

    class Failing:
        async def broadcast(self, *args, **kwargs):
            raise RuntimeError("socket gone")

    class Slow:
        async def broadcast(self, *args, **kwargs):
            await asyncio.sleep(30)

    for hub in (Broken(), Failing(), Slow()):
        app, seclog = make_app(live=SimpleNamespace(hub=hub))
        app.state.audit = Broken()
        with TestClient(app) as c:
            started = time.monotonic()
            r = c.post("/api/op")
            assert r.status_code == 401 and r.json() == {"detail": auth.DENIED}
            assert time.monotonic() - started < 2, type(hub).__name__   # scheduled, never awaited
        assert [e["kind"] for e in seclog.events()] == ["auth_failed"]


# ---------- bounds ----------

def test_alerts_per_minute_are_capped_but_events_are_kept():
    clock = FakeClock()
    app, seclog = make_app(SecurityLog(clock=clock, alerts_per_min=3))
    c = TestClient(app)
    for n in range(5):   # distinct paths, so no repeat folds them together
        c.post(f"/api/op/{n}")
    assert len(seclog.events()) == 5 and len(security_audit(app)) == 3
    clock.t += 61
    c.post("/api/op/next")
    assert len(security_audit(app)) == 4


def test_memory_cap_holds():
    seclog = SecurityLog(clock=FakeClock())
    for n in range(600):
        seclog.record("auth_failed", "POST", f"/api/op/{n}", ("203.0.113.7", 1), "401")
    events = seclog.events()
    assert len(events) == len(seclog) == security_log.CAP == 500
    assert (events[0]["id"], events[-1]["id"]) == (101, 600)
    assert len(seclog._open) <= 500   # the repeat index is bounded too

    app, small = make_app(SecurityLog(cap=5))
    c = TestClient(app)
    for n in range(8):
        c.post(f"/api/op/{n}")
    assert [e["path"] for e in small.events()] == [f"/api/op/{n}" for n in range(3, 8)]


def test_records_from_many_threads_add_up():
    seclog = SecurityLog(clock=FakeClock())
    go = threading.Barrier(8)

    def hammer(i: int) -> None:
        go.wait(timeout=10)
        for _ in range(500):
            seclog.record("auth_failed", "POST", "/api/op", ("203.0.113.7", 1), "401")
            seclog.record("lockout", "POST", f"/api/t{i}", ("203.0.113.7", 1), "429")
            seclog.events(since=10 ** 9)

    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)   # switch threads as often as possible, so a missing lock would show
    try:
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        sys.setswitchinterval(switch)
    events = seclog.events()
    counts = {e["path"]: e["count"] for e in events}
    assert counts["/api/op"] == 4000 and all(counts[f"/api/t{i}"] == 500 for i in range(8))
    assert len({e["id"] for e in events}) == len(events) == 9


def test_install_twice_is_one_log():
    app, seclog = make_app()
    assert security_log.install(app) is seclog
    TestClient(app).post("/api/op")
    assert [e["count"] for e in seclog.events()] == [1]
