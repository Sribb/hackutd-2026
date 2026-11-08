"""HTTP webhook connector.

Readings come in: a device or gateway POSTs them to /api/live/readings/{connector id}
as {"readings": [{"asset_id", "signal", "value"} | {"address", "value"}]}. Only the
signals listed in the blueprint are accepted, so a webhook can't silently override
an asset it doesn't own.

Commands go out: a confirmed command is POSTed as JSON to config.url:
    {"connector_id", "command", "asset_id", "value", "target_id", "address"}
Any 2xx counts as delivered, any other answer as FAILED, and no connection at all (connect
error or connect timeout) as FAILED with "Couldn't reach". Once the POST may have gone out,
a read timeout, a reset, or a dropped reply is `uncertain` (UNCERTAIN, never FAILED).

Config: url, timeout_s (default 5), token_env (an environment variable holding a
shared token: sent as "Authorization: Bearer <token>" on commands, and required as
the X-Freeboard-Token header on pushed readings).
"""
from __future__ import annotations

import hmac
import threading
from typing import Callable, Optional

import httpx

from ..fieldmap import SIGNALS, FieldMap
from .base import UNCERTAIN_DETAIL, Connector, Reading

# Errors raised before any byte of the command left: the gateway never saw it (FAILED).
# Anything else after the connection opened (ReadTimeout, WriteTimeout, ReadError, a reset,
# RemoteProtocolError) may have delivered it, so it is UNCERTAIN.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
            httpx.InvalidURL, httpx.LocalProtocolError, httpx.ProxyError)


class WebhookConnector(Connector):
    kind = "webhook"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float],
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        super().__init__(spec, fmap, now)
        self.url = self.config.get("url")
        self.timeout = float(self.config.get("timeout_s", 5))
        self.transport = transport  # tests inject httpx.MockTransport
        self.by_address = {addr: key for key, addr in self.signals.items() if addr}
        self._inbox: list[Reading] = []
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    # ---- readings pushed in ----
    def authorized(self, token: Optional[str]) -> bool:
        want = self.secret("token")
        if self.config.get("token_env") and not want:
            return False  # a token is configured but missing from the environment: refuse
        return not want or hmac.compare_digest(want, token or "")

    def push(self, readings: list[dict]) -> dict:
        accepted, rejected = [], []
        for r in readings:
            key = None
            if r.get("address") in self.by_address:
                key = self.by_address[r["address"]]
            elif (r.get("asset_id"), r.get("signal")) in self.signals:
                key = (r["asset_id"], r["signal"])
            if key is None or key[1] not in SIGNALS or "value" not in r:
                rejected.append({**r, "error": "not a signal this connector declares in the blueprint"})
                continue
            with self._lock:
                self._inbox.append(self.reading(key[0], key[1], r["value"]))
            accepted.append(f"{key[0]}.{key[1]}")
        return {"accepted": accepted, "rejected": rejected}

    def read(self) -> list[Reading]:
        with self._lock:
            out, self._inbox = self._inbox, []
        return out

    # ---- commands out ----
    def send(self, command: dict) -> dict:
        if not self.url:
            return {"ok": False, "detail": f"{self.id} has no url configured for commands."}
        address = self.commands.get((command.get("asset_id"), command.get("type")))
        body = {"connector_id": self.id, "command": command.get("type"), "asset_id": command.get("asset_id"),
                "value": command.get("value"), "target_id": command.get("target_id"), "address": address}
        headers = {}
        token = self.secret("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                r = client.post(self.url, json=body, headers=headers)
            if 200 <= r.status_code < 300:
                self.last_error = None
                return {"ok": True, "detail": f"Delivered to {self.id} (HTTP {r.status_code})."}
            self.last_error = f"HTTP {r.status_code}"
            return {"ok": False, "detail": f"{self.id} answered HTTP {r.status_code}."}
        except NOT_SENT as e:
            # Nothing left: no connection was made (or the request could not be built).
            self.last_error = str(e) or type(e).__name__
            return {"ok": False, "detail": f"Couldn't reach {self.id}: {e or type(e).__name__}"}
        except Exception as e:  # noqa: BLE001 - a dead endpoint must never break the console
            # The POST may have gone out (a read timeout, a reset, a dropped reply): the gateway
            # may have acted on it, so it is UNCERTAIN, never FAILED, and nothing re-sends it.
            self.last_error = str(e) or type(e).__name__
            return {"ok": False, "uncertain": True,
                    "detail": f"{self.id}: POST to {self.url} {UNCERTAIN_DETAIL} ({type(e).__name__}: {e})."}

    def status(self) -> dict:
        missing_token = bool(self.config.get("token_env")) and not self.secret("token")
        problems = []
        if missing_token:
            problems.append(f"environment variable {self.config['token_env']} is not set")
        if self.last_error:
            problems.append(f"last command failed: {self.last_error}")
        detail = "; ".join(problems) or (
            f"receiving {len(self.signals)} signals" + (", sending commands" if self.url else ", no command url"))
        return {"id": self.id, "kind": self.kind, "available": not missing_token, "detail": detail}
