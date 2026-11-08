"""The live layer's read routes in dataset mode (#153): GET /api/alerts and GET /api/live/state.

In dataset mode the console's alarm list and live readings come from the EOG dataset at the
Service's clock cursor, not from the demo simulator (which legacy_api pauses meanwhile). Shapes
are the live layer's (docs/CONTRACT.md "Live layer"), filled from the same Service as /api/eog:

  alerts      one per ongoing event, in the /api/eog/alerts order. A pump failure is an ALARM
              whose consequence is its tank's shutdown (the alert's plan.countdown trips_at, the
              p10 to p90 band, the wells that go down, the do-nothing cost). A leak or a worn
              pump is a WARNING that asks for an inspection.
  live state  every asset at the cursor with its readings and their real age in data minutes.
              A series with no reading at the cursor row is stale and the asset is UNKNOWN
              unless it is in ALARM; an asset the dataset has no readings for at all is UNKNOWN
              too. Never OK without a reading. Trucks are OK in their yard (the recording
              dispatches none).
"""
from __future__ import annotations

import math
from typing import Optional

from .. import detect as live_detect
from . import service
from .dataset import Dataset
from .legacy import forecast, hhmm, state_payload
from .model import iso

KIND = {"pump_failure": "PUMP_FAILURE", "pipeline_leak": "PIPELINE_LEAK", "pump_degradation": "PUMP_WORN"}
SCAN_ROWS = 2016              # how far back to look for an asset's last reading (a week of 5-min rows)


def live_t(ds: Dataset, ts: float) -> float:
    """The live layer's minute counter: data minutes since the dataset's first row."""
    return round((float(ts) - ds.start) / 60.0, 2)


def alerts_payload(ds: Dataset, view: Dataset, acks: Optional[dict] = None,
                   svc: Optional[service.Service] = None) -> dict:
    svc = svc or service.SERVICE
    acks = acks or {}
    now = view.now
    entries = svc.alert_entries(ds, view)
    fc = forecast(ds, view, now, [], svc=svc) if any(a["category"] == "pump_failure" for a in entries) else None
    lost = {e["asset_id"]: e for e in (fc or {}).get("events", [])}
    costs = (fc or {}).get("cost_by_tank", {})
    out = []
    for a in entries:
        cat = a["category"]
        ack = acks.get((ds.id, a["event_id"], a["onset"]))
        alert = {
            "id": a["event_id"], "event_id": a["event_id"], "asset_id": a["asset"], "kind": KIND.get(cat, "PROBLEM"),
            "category": cat, "system_id": a["system"], "facility_id": a["facility"],
            "severity": "ALARM" if cat == "pump_failure" else "WARNING",
            "message": a["title"] + ".", "t": iso(a["onset"]), "clock": hhmm(ds, a["onset"]), "active": True,
            "root_id": None, "priority": "P2" if cat == "pipeline_leak" else "P3",
            "minutes_to_consequence": None, "minutes_to_consequence_low": None, "consequence": None,
            "consequence_asset_id": None, "consequence_clock": None, "consequence_t": None, "usd_at_risk": 0,
            "shelved_until": None, "shelved_until_t": None, "shelve_reason": None, "shelved_by": None, "shelved_t": None,
            "acked": bool(ack), "acked_by": (ack or {}).get("by"), "action": a["action"].get("summary"),
            "approval": a.get("approval"),
        }
        if cat in ("pipeline_leak", "pump_degradation"):
            alert["consequence"] = f"Send an inspector to {a['asset']}."
        cd = a.get("countdown")
        if cat == "pump_failure" and cd:
            tank = cd["tank_id"]
            trip = now if cd["tripped"] else cd["trips_at"]
            if trip is None:
                alert.update(priority="P3", consequence=f"{tank} stays under shutdown until the repair at "
                                                        f"{hhmm(ds, cd['repair_end'])}.", consequence_asset_id=tank)
            else:
                m = int(math.floor((trip - now) / 60.0 + 0.5))
                h = cd.get("hours_to_limit") or {}
                spread = max(0.0, (h.get("p50") or 0) - (h.get("p10") or 0)) * 60 if h else 0.0
                low = max(0, int(math.floor(m - spread + 0.5)))
                wells = (lost.get(tank) or {}).get("wells_lost", [])
                what = (f"{tank} is at shutdown: its wells are shut in until the repair at {hhmm(ds, cd['repair_end'])}."
                        if cd["tripped"] else
                        f"{tank} reaches shutdown at {hhmm(ds, trip)}: {len(wells)} well{'s' if len(wells) != 1 else ''} "
                        f"go down.")
                alert.update(priority=live_detect.priority_for(low), minutes_to_consequence=m,
                             minutes_to_consequence_low=low, consequence=what, consequence_asset_id=tank,
                             consequence_clock=hhmm(ds, trip), consequence_t=live_t(ds, trip),
                             usd_at_risk=int(round((costs.get(tank) or {}).get("total_usd") or 0)), wells_lost=wells)
        out.append(alert)
    return {"mode": "dataset", "dataset": ds.id, "clock": hhmm(ds, now), "clock_ts": iso(now), "now": now,
            "alerts": out}


def _last_reading(view: Dataset, asset_id: str) -> Optional[int]:
    """ts_unix of the newest visible row with any reading for this asset, or None."""
    best = None
    for m in view.metrics(asset_id):
        col = view.values(asset_id, m)
        for i in range(view.end - 1, max(-1, view.end - 1 - SCAN_ROWS), -1):
            if not math.isnan(col[i]):
                ts = int(view.times[i])
                best = ts if best is None or ts > best else best
                break
    return best


def live_state_payload(ds: Dataset, view: Dataset, at: int, svc: Optional[service.Service] = None) -> dict:
    svc = svc or service.SERVICE
    st = state_payload(ds, view, at, svc)
    now = view.now
    clock = svc.clock_payload(ds)
    step_min = (clock.get("step_s") or 300) / 60.0
    assets = {}
    for aid, a in st["assets"].items():
        e = dict(a)
        if a["kind"] == "truck":
            e.update(stale=False, age_min=None, last_reading_t=None)
            assets[aid] = e
            continue
        last = _last_reading(view, aid)
        age = None if last is None else round((now - last) / 60.0, 2)
        e["readings"] = {m: view.value_at(aid, m) for m in ds.metrics(aid)}
        missing = [m for m, v in e["readings"].items() if v is None]
        e.update(age_min=age, last_reading_t=iso(last), stale=last is None or age > step_min or bool(missing))
        if e["stale"] and e["status"] != "ALARM":
            e["status"] = "UNKNOWN"
            note = ("The dataset has no readings for this asset, so it can't be called safe." if last is None else
                    f"No current {', '.join(missing) or 'reading'} (last reading {hhmm(ds, last)}). Treat as unknown, not safe.")
            e["reason"] = f"{e['reason']} {note}" if e.get("reason") else note
        assets[aid] = e
    return {"mode": "dataset", "dataset": ds.id, "clock": st["clock"], "clock_ts": st["clock_ts"], "now": now, "at": at,
            "t": live_t(ds, now), "assets": assets, "incident": st["incident"], "warnings": st["warnings"],
            "connectors": [{"id": "dataset", "kind": "dataset", "status": "connected",
                            "detail": f"{ds.id} at {st['clock_ts']}"}],
            "speed": clock["speed"], "paused": not clock["playing"]}
