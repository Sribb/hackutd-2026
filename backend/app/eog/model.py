"""Shared types for dataset mode: events, plan actions and plans.

Every module in backend/app/eog speaks these types, and `to_dict()` gives the shapes the API
returns. Event.to_events_json() is the SPEC.md "events.json" shape exactly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

CATEGORIES = ("pump_failure", "pump_degradation", "pipeline_leak", "other")
ACTION_TYPES = ("dispatch_truck", "shut_in_wells", "field_inspection", "none")


def iso(ts: Optional[int]) -> Optional[str]:
    """ts_unix -> "2026-10-11T21:00:00Z" (UTC). None stays None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(x: float) -> str:
    """$905, $1,958: the way the README writes costs."""
    return f"${round(x):,}"


@dataclass
class Event:
    """One problem found in the readings.

    The detector fills the first block. For pump failures the countdown block is filled by
    the countdown step (detect or plan), from the readings at the view's "now".
    """
    id: str                       # "EV-0001", numbered in onset order within the dataset view
    category: str                 # one of CATEGORIES
    system: str
    facility: str
    asset: str                    # the pump for pump problems, the pipeline for a leak
    onset: int                    # ts_unix of the first row the problem shows in
    end: Optional[int]            # ts_unix the problem ended (resolved), None while ongoing
    status: str                   # "ongoing" or "resolved"
    confidence: float             # 0..1, how sure the detector is
    evidence: list[str] = field(default_factory=list)   # plain-English reasons
    detected_at: Optional[int] = None   # first row at which the detector would have raised it
    # ---- pump failures only ----
    level_at_onset_pct: Optional[float] = None
    level_pct: Optional[float] = None           # tank level at "now" (ongoing) or at the end (resolved)
    hours_to_limit: Optional[dict] = None       # {"p50": h, "p10": h, "p90": h}, ongoing only
    trips_at: Optional[int] = None              # when the tank reaches shutdown if nobody acts; None if it never does before the repair
    repair_end: Optional[int] = None            # onset + expected repair hours
    tripped: bool = False                       # the tank already reached shutdown (wells shut in)
    lost_revenue_per_hour: Optional[float] = None   # whole facility, used for the alerts tie-break

    def to_dict(self) -> dict:
        d = asdict(self)
        d["onset_ts"], d["end_ts"] = iso(self.onset), iso(self.end)
        d["trips_at_ts"], d["repair_end_ts"] = iso(self.trips_at), iso(self.repair_end)
        return d

    def to_events_json(self, actions: Optional[list[dict]] = None, est_cost: Optional[float] = None) -> dict:
        """SPEC.md events.json shape. hours_to_limit only for ongoing pump failures."""
        out = {
            "event_id": self.id,
            "system_id": self.system,
            "facility_id": self.facility,
            "asset_id": self.asset,
            "category": self.category,
            "status": self.status,
            "onset_ts": iso(self.onset),
            "onset_unix": self.onset,
            "end_ts": iso(self.end),
            "end_unix": self.end,
            "confidence": round(float(self.confidence), 2),
            "evidence": list(self.evidence),
            "level_pct": None if self.level_pct is None else round(self.level_pct, 1),
            "hours_to_limit": (self.hours_to_limit if (self.category == "pump_failure" and self.status == "ongoing")
                               else None),
            "recommended_actions": actions if actions is not None else (
                [{"type": "field_inspection", "asset_id": self.asset}]
                if self.category in ("pump_degradation", "pipeline_leak") else [{"type": "none"}]),
        }
        if est_cost is not None:
            out["est_cost"] = round(est_cost, 2)
        return out


@dataclass
class Plan:
    """The recommended response to one pump failure, or one alternative to it.

    `actions` use the events.json action shapes:
      {"type": "dispatch_truck", "facility_id", "trucks", "trips_per_truck"}
      {"type": "shut_in_wells", "wells": [...], "start_after_min": N}
      {"type": "none"}            (accept the automatic shut-in, or nothing is needed)
    """
    event_id: str
    facility_id: str
    system: str
    start: int                       # ts_unix the plan starts (now, or the onset in history mode)
    actions: list[dict]
    cost: float                      # truck trips + lost revenue from shut-ins (including an automatic shut-in)
    trips: bool                      # the tank still reaches shutdown under this plan
    trip_at: Optional[int]           # when, if it does
    peak_level_pct: float            # highest level before the repair ends
    summary: str                     # "1 truck, 1 trip (~$905)", in the answer key's words
    truck_cost: float = 0.0
    shut_in_cost: float = 0.0
    auto_shut_in_cost: float = 0.0
    trucks_free: Optional[int] = None    # trucks free in the system when this plan starts
    fleet_size: Optional[int] = None
    shares_with: list[str] = field(default_factory=list)   # other events planned jointly
    arrivals: list[int] = field(default_factory=list)      # ts_unix of each truck trip arrival
    busy_until: list[int] = field(default_factory=list)    # per truck used, when it is free again
    expires_at: Optional[int] = None     # Last Call: last start time at which this plan still works
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start_ts"], d["trip_at_ts"], d["expires_at_ts"] = iso(self.start), iso(self.trip_at), iso(self.expires_at)
        d["arrivals_ts"] = [iso(t) for t in self.arrivals]
        return d
