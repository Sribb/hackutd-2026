"""PI Web API connector (kind "piwebapi"): current values from an AVEVA PI System, read only.

Each declared signal's address is a PI WebId. A daemon thread polls all of them with
one request, GET {base_url}/streamsets/value?webId=...&webId=..., every poll_ms, and
puts each mapped value in an Inbox that read() drains. read(), status() and close()
never wait on the network.

    {"id": "CONN_PI", "kind": "piwebapi",
     "config": {"base_url": "https://pi.example.com/piwebapi",
                "auth": "basic", "username": "freeboard", "password_env": "FREEBOARD_CONN_PI_PASSWORD",
                "verify_tls": true, "ca_path_env": "FREEBOARD_CONN_PI_CA",
                "poll_ms": 5000, "timeout_s": 5, "max_age_s": 300},
     "signals": [{"asset_id": "TANK_3", "signal": "level_pct", "address": "F1DP...WebId"}]}

Config:
- base_url: https, unless the host is 127.0.0.1, localhost, or ::1 (a local fake). No
  user name or password in the URL.
- auth: "basic" (username, password_env), "bearer" (token_env), or "none". Without
  `auth`, basic when username is set, bearer when token_env is set, else none. Every
  *_env key must name a FREEBOARD_CONN_ variable; the values are read on connect and
  never logged or shown.
- verify_tls (true): check the server certificate. ca_path_env names a variable that
  holds the path of a CA bundle, for a site CA. false is allowed, and status() says so.
- poll_ms (5000), timeout_s (5) per request, max_age_s (300): the oldest PI timestamp
  read as current. PI stamps a value when it last changed, so a tag that holds still
  longer than max_age_s goes stale; raise max_age_s for such tags, or set a rule's
  own max_age_s in signal_map.

A value is used only when PI says Good is true, Questionable is false, Substituted is
false, the value is not a digital state (an object such as {"Name": "Bad Input"}),
and its Timestamp is no older than max_age_s. A digital state with IsSystem false is a
real enumerated state (such as Open or Closed) and is read for running and position.
Anything else gives no reading, and self.quality says why. A failed poll marks every
signal comm_lost and status() unavailable; the thread retries forever with backoff.

send() always refuses: this connector is read only. A command routed here fails, so a
person sees it did not reach the field.

backfill(minutes) reads the PI archive (streams/{webId}/recorded) so the readings
history (history.py) starts with the last hour instead of empty. See its docstring for
the clock mapping.
"""
from __future__ import annotations

import copy
import math
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from . import signalmap as sm
from .base import Connector, Reading

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_MAX_AGE_S = 300.0
AUTH_MODES = ("basic", "bearer", "none")


def parse_pi_time(text: Any) -> Optional[float]:
    """A PI Web API timestamp ("2026-11-07T08:00:00.1234567Z") as Unix seconds, or None.
    PI writes up to seven fraction digits; Python reads six, so the rest is dropped.
    A timestamp without a zone is read as UTC, which is what PI sends."""
    if not isinstance(text, str) or len(text) < 19:
        return None
    s = text.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    head, dot, rest = s.partition(".")
    if dot:
        digits = ""
        while rest and rest[0].isdigit():
            digits, rest = digits + rest[0], rest[1:]
        s = head + ("." + digits[:6] if digits else "") + rest
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def pi_value(signal: str, obj: Any) -> tuple[Any, str, str, Optional[float]]:
    """(raw, quality, detail, unix_ts) for one PI value object, before SignalMap.

    quality is GOOD only when Good is true, Questionable and Substituted are false, and
    the value is a number, text, a bool, or a non-system digital state (read as its
    Name, for running and position only)."""
    if not isinstance(obj, dict):
        return None, sm.BAD, "PI sent no value object", None
    ts = parse_pi_time(obj.get("Timestamp"))
    if obj.get("Good") is not True:
        return None, sm.BAD, "PI marks it not Good", ts
    if obj.get("Questionable") is True:
        return None, sm.UNCERTAIN, "PI marks it Questionable", ts
    if obj.get("Substituted") is True:
        return None, sm.UNCERTAIN, "PI marks it Substituted (not a measured value)", ts
    raw = obj.get("Value")
    if isinstance(raw, dict):
        name = raw.get("Name")
        label = name if isinstance(name, str) and len(name) <= 60 else "a digital state"
        if raw.get("IsSystem") is not False:
            return None, sm.BAD, f"PI system digital state {label!r}", ts
        if signal not in sm.BOOLEAN:
            return None, sm.BAD, f"digital state {label!r} where a number was expected", ts
        raw = name
    elif isinstance(raw, list):
        return None, sm.BAD, "PI sent a list where one value was expected", ts
    return raw, sm.GOOD, "", ts


class PiWebApiConnector(Connector):
    kind = "piwebapi"

    def __init__(self, spec: dict, fmap, now) -> None:
        super().__init__(spec, fmap, now)
        sm.check_env_names(self.config)
        c = self.config
        self.base_url = self._base_url(c.get("base_url"))
        self.auth = self._auth_mode(c)
        self.verify_tls = c.get("verify_tls", True)
        if not isinstance(self.verify_tls, bool):
            raise ValueError("config verify_tls must be true or false")
        self.poll_s = self._num(c, "poll_ms", 5000, lo=50) / 1000.0
        self.timeout_s = self._num(c, "timeout_s", 5, lo=0.1)
        spec_for_map = copy.deepcopy(spec)
        spec_for_map.setdefault("config", {})
        if spec_for_map["config"].get("max_age_s") is None:
            spec_for_map["config"]["max_age_s"] = DEFAULT_MAX_AGE_S
        self.smap = sm.SignalMap(spec_for_map)
        self.max_age_s = self.smap.max_age_s
        self.by_webid: dict[str, list[tuple]] = {}
        for key, addr in self.signals.items():
            if not isinstance(addr, str) or not addr.strip():
                raise ValueError(f"signals: {key[0]}.{key[1]} needs a PI WebId as its address")
            self.by_webid.setdefault(addr.strip(), []).append(key)
        if not self.by_webid:
            raise ValueError("declare at least one signal with a PI WebId address")
        self.inbox = sm.Inbox()
        self._lock = threading.Lock()
        self.quality: dict[tuple, dict] = {
            key: {"quality": sm.NULL, "detail": "not read yet", "t": self.now()} for key in self.signals}
        self._available = False
        self._detail = f"connecting to {self.host}"
        self.last_backfill: dict = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"piwebapi-{self.id}", daemon=True)
        self._thread.start()

    # ---------- config ----------
    @staticmethod
    def _base_url(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config base_url is required, for example https://pi.example.com/piwebapi")
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("config base_url must be an http(s) URL")
        if parts.username or parts.password:
            raise ValueError("config base_url must not carry a user name or password; use auth and password_env")
        if parts.scheme != "https" and parts.hostname not in LOCAL_HOSTS:
            raise ValueError("config base_url must use https unless the host is 127.0.0.1 or localhost")
        if parts.query or parts.fragment:
            raise ValueError("config base_url must not have a query or fragment")
        return value.strip().rstrip("/")

    @staticmethod
    def _auth_mode(c: dict) -> str:
        mode = c.get("auth")
        if mode is None:
            mode = "basic" if c.get("username") else "bearer" if c.get("token_env") else "none"
        if mode not in AUTH_MODES:
            raise ValueError(f"config auth must be one of {', '.join(AUTH_MODES)}")
        if mode == "basic" and not (isinstance(c.get("username"), str) and c["username"] and c.get("password_env")):
            raise ValueError("auth basic needs username and password_env")
        if mode == "bearer" and not c.get("token_env"):
            raise ValueError("auth bearer needs token_env")
        return mode

    @staticmethod
    def _num(c: dict, key: str, default: float, lo: float) -> float:
        v = c.get(key, default)
        x = None if isinstance(v, bool) else sm.finite(v)
        if x is None or x < lo:
            raise ValueError(f"config {key} must be a number of at least {lo:g}")
        return x

    @property
    def host(self) -> str:
        parts = urlsplit(self.base_url)
        return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)

    def _client(self):
        """A new httpx.Client with auth and TLS from config, or raise RuntimeError with a
        reason that names no secret. Each thread that talks to PI makes its own."""
        httpx, why = sm.import_optional("httpx", "httpx")
        if httpx is None:
            raise RuntimeError(why)
        missing = sm.missing_env(self.config)
        if missing:
            raise RuntimeError(f"{missing} is not set")
        auth, headers = None, {"Accept": "application/json"}
        if self.auth == "basic":
            auth = httpx.BasicAuth(self.config["username"], sm.env_secret(self.config, "password") or "")
        elif self.auth == "bearer":
            token = (sm.env_secret(self.config, "token") or "").strip()
            if not token or not all("!" <= ch <= "~" for ch in token):
                # A header with a CR, a space, or a non-ASCII letter makes httpx raise an
                # error that quotes the whole header, token included. Refuse it here, by name.
                raise RuntimeError(f"the token in {self.config['token_env']} is empty or has characters "
                                   f"an HTTP header can't carry (only visible ASCII is allowed)")
            headers["Authorization"] = "Bearer " + token
        verify: Any = self.verify_tls
        if self.base_url.startswith("http://"):
            verify = False      # plain http to a local fake: no TLS, so skip loading the CA store
        elif self.verify_tls and self.config.get("ca_path_env"):
            try:
                verify = ssl.create_default_context(cafile=sm.env_secret(self.config, "ca_path"))
            except (OSError, ssl.SSLError) as e:
                raise RuntimeError(f"the CA bundle named by {self.config['ca_path_env']} can't be read "
                                   f"({type(e).__name__})") from None
        return httpx.Client(auth=auth, headers=headers, verify=verify, timeout=self.timeout_s,
                            follow_redirects=False)

    def _why(self, e: BaseException) -> str:
        """A failure as text with no secret in it: the status code for an HTTP error, our
        own RuntimeError text, or the message of a network error (connect, read, timeout,
        TLS, or the server breaking the protocol). Any other exception shows only its type:
        an error about what we sent, such as httpx's LocalProtocolError or a
        UnicodeEncodeError, can quote a request header, and the header carries the login.
        Secret values are also cut out of whatever text is kept."""
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None)
        if code in (401, 403):
            return f"PI Web API refused the login (HTTP {code})"
        if code is not None:
            return f"PI Web API answered HTTP {code}"
        if isinstance(e, RuntimeError):
            return self._redact(str(e))
        if isinstance(e, ValueError) and type(e).__name__ == "JSONDecodeError":
            return "PI Web API sent a body that isn't JSON"
        httpx, _ = sm.import_optional("httpx", "httpx")
        safe: tuple = (ssl.SSLError,)
        if httpx is not None:
            safe += (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
        msg = str(e).splitlines()[0][:160] if isinstance(e, safe) and str(e) else ""
        msg = self._redact(msg)
        return f"{type(e).__name__}: {msg}" if msg else type(e).__name__

    def _redact(self, text: str) -> str:
        """text with every configured secret value, plain or as Python escapes it, cut out."""
        for key in ("password", "token"):
            value = sm.env_secret(self.config, key)
            if not value:
                continue
            for form in sorted({value, value.strip(), repr(value)[1:-1]}, key=len, reverse=True):
                if len(form) >= 4 and form in text:
                    text = text.replace(form, "***")
        return text

    # ---------- the I/O thread ----------
    def _run(self) -> None:
        backoff = sm.Backoff(initial_s=0.5, max_s=30.0)
        client = None
        params = [("webId", w) for w in self.by_webid]
        url = self.base_url + "/streamsets/value"
        while not self._stop.is_set():
            try:
                if client is None:
                    client = self._client()
                resp = client.get(url, params=params)
                if resp.status_code not in (200, 207):
                    resp.raise_for_status()
                    raise RuntimeError(f"PI Web API answered HTTP {resp.status_code}")
                body = resp.json()
                if not (isinstance(body, dict) and isinstance(body.get("Items"), list)):
                    # A proxy or a wrong path can answer 200 with some other JSON. That is
                    # not PI, so the link counts as down, not as connected with no values.
                    raise RuntimeError("the answer has no Items list, so it isn't from PI Web API "
                                       "(check base_url)")
                if self._stop.is_set():
                    break
                self._take(body)
                backoff.reset()
                with self._lock:
                    if self._stop.is_set():
                        break
                    self._available = True
                    self._detail = (f"polling {len(self.by_webid)} PI points on {self.host} every "
                                    f"{self.poll_s * 1000:g} ms ({self.auth} auth"
                                    f"{', TLS NOT VERIFIED' if not self.verify_tls else ''}); read only")
                delay = self.poll_s
            except Exception as e:  # noqa: BLE001 - every failure means: link lost, try again
                if self._stop.is_set():
                    break
                self._lost(self._why(e))
                if client is not None:
                    sm.close_later(client.close)
                    client = None
                delay = backoff.next_delay()
            self._stop.wait(delay)
        if client is not None:
            sm.close_later(client.close)

    def _set_quality(self, key: tuple, m: sm.Mapped) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self.quality[key] = {"quality": m.quality, "detail": m.detail, "t": self.now()}

    def _take(self, body: Any) -> None:
        items = body.get("Items") if isinstance(body, dict) else None
        found: dict[str, Any] = {}
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("WebId"), str):
                found[item["WebId"]] = item
        wall = time.time()
        for webid, keys in self.by_webid.items():
            item = found.get(webid)
            for aid, sig in keys:
                if item is None:
                    m = sm.Mapped(None, sm.BAD, "PI did not return this WebId")
                else:
                    raw, q, detail, ts = pi_value(sig, item.get("Value"))
                    age = (wall - ts) if ts is not None else "missing"
                    if q != sm.GOOD:
                        m = sm.Mapped(None, q, detail)
                    else:
                        m = self.smap.apply(aid, sig, raw, age_s=age)
                self._set_quality((aid, sig), m)
                self.inbox.put(aid, sig, m)

    def _lost(self, why: str) -> None:
        lost = sm.Mapped(None, sm.COMM_LOST, why)
        for aid, sig in self.signals:
            self._set_quality((aid, sig), lost)
            self.inbox.put(aid, sig, lost)
        with self._lock:
            self._available = False
            self._detail = f"{self.host}: {why}; retrying"

    # ---------- the interface ----------
    def read(self) -> list[Reading]:
        if self._stop.is_set():
            # A poll that was already in flight when close() ran may still fill the inbox.
            self.inbox.clear()
            return []
        return [self.reading(aid, sig, v) for aid, sig, v in self.inbox.drain()]

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": f"PI Web API connector {self.id} is read only; nothing was sent."}

    def status(self) -> dict:
        with self._lock:
            available, detail = self._available, self._detail
        return {"id": self.id, "kind": self.kind, "available": available, "detail": detail}

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._available = False
            self._detail = "closed"
        self.inbox.clear()

    # ---------- the archive ----------
    def backfill(self, minutes: float) -> list[tuple[str, str, list[tuple[float, Any]]]]:
        """The PI archive for the last `minutes`: [(asset_id, signal, [(t, value), ...])].

        One GET streams/{webId}/recorded?startTime=*-<m>m&endTime=* per WebId, each
        bounded by timeout_s. It blocks, so call it from a worker thread, never the event
        loop. Each value passes the same checks as a live one (Good, not Questionable or
        Substituted, no system digital state, scale and signal_map), except the age
        limit, since an archive value is old by design. A value that fails is left out,
        so the history shows a gap there, never a number.

        Clock: the live clock is a demo clock, not the wall clock, so a PI timestamp is
        placed by its age. A value PI stamped `a` real minutes ago lands at live minute
        now() - a: the newest archive value sits at the live "now", and an hour of
        archive fills the hour before it, minute for minute, whatever the live speed.
        Values stamped in the future are left out. Signals whose request fails are
        missing from the result; last_backfill says which and why. Recorder.backfill
        then keeps only the values from before recording started, so the archive never
        fills a minute the console itself showed as stale."""
        m = sm.finite(minutes)
        if m is None or m <= 0:
            return []
        span = max(1, int(math.ceil(m)))
        out: list[tuple[str, str, list]] = []
        failed: dict[str, str] = {}
        try:
            client = self._client()
        except Exception as e:  # noqa: BLE001
            self.last_backfill = {"signals": 0, "points": 0, "failed": {"*": self._why(e)}}
            return []
        try:
            for webid, keys in self.by_webid.items():
                try:
                    resp = client.get(f"{self.base_url}/streams/{webid}/recorded",
                                      params={"startTime": f"*-{span}m", "endTime": "*"})
                    resp.raise_for_status()
                    body = resp.json()
                except Exception as e:  # noqa: BLE001
                    failed[webid] = self._why(e)
                    continue
                items = body.get("Items") if isinstance(body, dict) else None
                wall, now_t = time.time(), float(self.now())
                for aid, sig in keys:
                    pts: list[tuple[float, Any]] = []
                    for obj in items if isinstance(items, list) else []:
                        raw, q, _, ts = pi_value(sig, obj)
                        if q != sm.GOOD or ts is None or ts > wall + 1.0 or wall - ts > span * 60.0 + 60.0:
                            continue
                        mv = self.smap.apply(aid, sig, raw)
                        if mv.good:
                            pts.append((round(now_t - (wall - ts) / 60.0, 3), mv.value))
                    pts.sort(key=lambda p: p[0])
                    out.append((aid, sig, pts))
        finally:
            sm.close_later(client.close)
        self.last_backfill = {"signals": len(out), "points": sum(len(p) for _, _, p in out), "failed": failed}
        return out
