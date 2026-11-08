"""Text and voice alerts to a phone (#160): the judge's number from /start, or an operator's.

Twilio's REST API, used only when the server's .env has all three of TWILIO_ACCOUNT_SID,
TWILIO_AUTH_TOKEN and TWILIO_FROM (a Twilio number, +1...). Without them every route still
answers, says no provider is set, and the console falls back to the phone pager.

  GET    /api/phone/status          {provider: "twilio" | "none", subscribers}
  POST   /api/phone/subscribe       {phone}       -> {id, masked, provider}      operator
  DELETE /api/phone/subscribe/{id}                -> {ok}                         operator
  POST   /api/phone/alert           {alert_id, text, call?} -> {provider, queued} operator

Safety:
- Subscribing and sending need the operator token, so a public link can't make the server
  text or call any number.
- Numbers live in memory only (gone on restart), expire after SUB_TTL_S, and are masked in
  every response and log line. Tokens are never logged.
- Each alert_id goes to each number once; each number gets at most MAX_PER_SUB messages.
- The text is plain: control characters are stripped, it is capped at MAX_TEXT characters
  and always starts with "gaia: ". The voice call says the same words.
"""
from __future__ import annotations

import base64
import html
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import OPERATOR, Caller, require_role

log = logging.getLogger("freeboard.phone")

SUB_TTL_S = 4 * 3600
MAX_SUBS = 50
MAX_PER_SUB = 12
MAX_TEXT = 300
# The voice that reads a call (Twilio <Say>). Amazon Polly's neural voice reads times and money
# naturally; TWILIO_VOICE overrides it (for example "alice").
VOICE = os.environ.get("TWILIO_VOICE", "Polly.Joanna-Neural").strip() or "alice"
E164_US = re.compile(r"^\+1[2-9]\d{2}[2-9]\d{6}$")

router = APIRouter(prefix="/api/phone", tags=["phone alerts"])
_lock = threading.Lock()
_subs: dict[str, dict] = {}   # id -> {phone, created, sent: set(alert ids), count}


def provider() -> str:
    return "twilio" if all(os.environ.get(k, "").strip() for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")) else "none"


def mask(phone: str) -> str:
    """+12145550134 -> +1 214 ***-0134"""
    return f"+1 {phone[2:5]} ***-{phone[-4:]}" if E164_US.match(phone or "") else "***"


def normalize(raw: Any) -> Optional[str]:
    """A US number in any common form -> E.164 (+1XXXXXXXXXX), or None."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    phone = f"+1{digits}"
    return phone if E164_US.match(phone) else None


def clean_text(raw: Any) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(raw or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text.lower().startswith("gaia"):
        text = f"gaia: {text}"
    return text[:MAX_TEXT]


def spoken(text: str) -> str:
    """The text message as a voice should say it: no "gaia:" tag and no texting shorthand.
    "gaia: S1 Pump C failed; ... in 4 h 24 min; ... $905 vs $5,023" ->
    "S1 Pump C failed. ... in 4 hours 24 minutes. ... $905 versus $5,023"."""
    s = re.sub(r"^gaia:\s*", "", text, flags=re.I)
    s = re.sub(r"\b0([1-9]):(\d\d)\b", r"\1:\2", s)  # 04:49 -> 4:49
    s = re.sub(r"\b(\d+) h\b", lambda m: m[1] + (" hour" if m[1] == "1" else " hours"), s)
    s = re.sub(r"\b(\d+) min\b", lambda m: m[1] + (" minute" if m[1] == "1" else " minutes"), s)
    s = re.sub(r"\bvs\.?(?=\s)", "versus", s).replace("~", "about ")
    s = re.sub(r"\s*;\s*", ". ", s).strip()
    return s[:1].upper() + s[1:]


def _prune(now: float) -> None:
    for k in [k for k, s in _subs.items() if now - s["created"] > SUB_TTL_S]:
        del _subs[k]


def _twilio(path: str, fields: dict) -> int:
    """POST a form to Twilio's 2010-04-01 API; returns the HTTP status (0 on no answer)."""
    sid = os.environ["TWILIO_ACCOUNT_SID"].strip()
    token = os.environ["TWILIO_AUTH_TOKEN"].strip()
    base = os.environ.get("TWILIO_API_BASE", "https://api.twilio.com").rstrip("/")  # tests point this at a fake
    url = f"{base}/2010-04-01/Accounts/{urllib.parse.quote(sid)}/{path}"
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Authorization": f"Basic {auth}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


def _send(phone: str, text: str, call: bool) -> dict:
    sender = os.environ["TWILIO_FROM"].strip()
    sms = _twilio("Messages.json", {"To": phone, "From": sender, "Body": text})
    voice = None
    if call:
        # The alert itself, said twice: what failed, when the tank shuts down, the cheapest fix and
        # its cost, then how to act on it. The same numbers as the text and the alert card.
        words = spoken(text)
        say = lambda s: f'<Say voice="{html.escape(VOICE)}">{html.escape(s)}</Say>'  # noqa: E731
        twiml = ("<Response>" + say("This is gaia, your night-shift agent, with a field alert.") + '<Pause length="1"/>'
                 + say(words) + '<Pause length="1"/>' + say("Again. " + words)
                 + say("Open gaia to see the plan. Nothing moves until a person holds to confirm.") + "</Response>")
        voice = _twilio("Calls.json", {"To": phone, "From": sender, "Twiml": twiml})
    ok = lambda s: s is not None and 200 <= s < 300  # noqa: E731
    log.info("phone alert to %s: text %s%s", mask(phone), "sent" if ok(sms) else f"failed ({sms})",
             "" if voice is None else f", call {'placed' if ok(voice) else f'failed ({voice})'}")
    return {"text": ok(sms), "call": None if voice is None else ok(voice)}


@router.get("/status")
def status() -> dict:
    with _lock:
        _prune(time.time())
        return {"provider": provider(), "subscribers": len(_subs)}


@router.post("/subscribe")
async def subscribe(request: Request, caller: Caller = Depends(require_role(OPERATOR))) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    phone = normalize((body or {}).get("phone"))
    if not phone:
        raise HTTPException(422, "phone must be a US number, like (214) 555-0134 or +12145550134.")
    now = time.time()
    with _lock:
        _prune(now)
        for k, s in _subs.items():
            if s["phone"] == phone:
                s["created"] = now
                return {"id": k, "masked": mask(phone), "provider": provider()}
        if len(_subs) >= MAX_SUBS:
            raise HTTPException(429, "Too many phones are signed up for alerts right now.")
        sub_id = secrets.token_urlsafe(9)
        _subs[sub_id] = {"phone": phone, "created": now, "sent": set(), "count": 0}
    return {"id": sub_id, "masked": mask(phone), "provider": provider()}


@router.delete("/subscribe/{sub_id}")
def unsubscribe(sub_id: str, caller: Caller = Depends(require_role(OPERATOR))) -> dict:
    with _lock:
        return {"ok": _subs.pop(sub_id, None) is not None}


@router.post("/alert")
async def alert(request: Request, caller: Caller = Depends(require_role(OPERATOR))) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body or {}
    alert_id = str(body.get("alert_id") or "").strip()[:120]
    if not alert_id:
        raise HTTPException(422, "alert_id is required, so each alert reaches a phone once.")
    text = clean_text(body.get("text"))
    if len(text) < 12:
        raise HTTPException(422, "text must say what happened.")
    call = bool(body.get("call", True))
    prov = provider()
    if prov == "none":
        with _lock:
            n = len(_subs)
        return {"provider": "none", "queued": 0, "subscribers": n,
                "detail": "No text or call provider is set on the server (TWILIO_* in .env), so use the phone pager."}
    with _lock:
        _prune(time.time())
        targets = []
        for s in _subs.values():
            if alert_id in s["sent"] or s["count"] >= MAX_PER_SUB:
                continue
            s["sent"].add(alert_id)
            s["count"] += 1
            targets.append(s["phone"])
    for phone in targets:
        threading.Thread(target=_send, args=(phone, text, call), daemon=True).start()
    return {"provider": prov, "queued": len(targets), "text": text}


def reset() -> None:
    """Tests: forget every subscription."""
    with _lock:
        _subs.clear()
