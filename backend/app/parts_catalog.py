"""Plain words for the parts of each asset, for Jarvis's describe_parts tool.

A copy of web/src/parts.js `partWords` (origin/67-demo-start), so Jarvis and the hover
tooltip name a part the same way. Keep the two in step: when a label changes there,
change it here. The part keys are the `<ID>_<key>` children in field.glb (docs/CONTRACT.md
"What's in field.glb"), plus the functional parts the Jarvis console spec asks the 3D
lane to split out (docs/JARVIS_CONSOLE.md section 3), marked `planned`.

    parts_for("pump", "PUMP_3", live_entry) ->
        {"kind": "pump", "parts": [{"key": "rotor", "label": "motor fan", "what": "...",
                                    "signal": "running", "value": False}, ...]}
"""
from __future__ import annotations

import re
from typing import Optional

# (key pattern, label, what) in parts.js GENERIC order: the first match wins.
GENERIC = [
    (r"^LEVEL$", "brine level", "The salt water inside the tank, drawn at the current level."),
    (r"^gauge$", "level gauge", "The float marker on the outside level board. It rides up and down with the brine."),
    (r"^lt$", "radar level sensor", "Reads the brine level from the top of the tank. The console's level comes from it."),
    (r"^lshh$", "high-level switch", "Trips the shutdown before the tank can overflow."),
    (r"^loadout$", "load-out stand", "Where a truck hooks up to drain the tank."),
    (r"^rotor$", "motor fan", "Spins while the pump runs. A still fan means no flow."),
    (r"^light$", "status light", "Shows the status color. The time of day never changes it."),
    (r"^wheel$", "handwheel", "Turned by hand to open or close the valve. It spins while a what-if changes it."),
    (r"^beam$", "walking beam", "Rocks up and down while the well produces."),
    (r"^crank$", "crank and counterweights", "Turn while the well produces. Still means shut in."),
    (r"^axle_(\d+)$", "wheels", "Spin while the truck drives."),
    (r"^beacon$", "job beacon", "Flashes amber while the truck is on a job."),
    (r"^headlights$", "head and tail lights", "On while the truck drives at night."),
    (r"^gate$", "fence gate", "The gate in the disposal well's fence."),
    (r"^lamps$", "light tower lamps", "Site lighting. On from dusk to dawn."),
    (r"^tower", "light tower", "Mast and footing for the site lights."),
    (r"^sensor$", "sensor", "Sends this asset's reading to the console."),
    (r"^rtu$", "remote terminal unit", "The field box that sends this asset's readings and takes confirmed commands."),
    (r"^(label|label_cap)$", "painted number", "The asset number painted where a driver can read it."),
    (r"^tag$", "ID tag", "The tag with the asset ID."),
    (r"^sign$", "sign", "The site sign."),
    (r"^glass$", "cab windows", "The truck's cab glass."),
    (r"^detail_tank$", "settling tank", "Lets solids settle out before the water is injected."),
    (r"concrete", "concrete footing", "The concrete the equipment stands on."),
    (r"gravel", "gravel pad", "The gravel pad around the equipment."),
    (r"pipe", "piping", "Pipes and fittings on this asset."),
    (r"rubber", "hoses and seals", "Rubber hoses, gaskets and seals."),
    (r"steel", "steelwork", "Frames, rails and fittings."),
]

# parts.js BY_KIND: kind-specific words that win over GENERIC.
BY_KIND = {
    "tank": {"tank": ("tank shell", "The steel tank. The cutaway shows the brine inside."),
             "steel": ("stairs and railings", "The stairs and the walkway on top.")},
    "pump": {"steel": ("pump and motor", "The transfer pump and the electric motor that drives it."),
             "body": ("transfer pump", "Moves the tank's water to the disposal line.")},
    "valve_d": {"steel": ("valve body", "The gate valve on the pump's discharge line.")},
    "valve_xo": {"steel": ("valve body", "The gate valve on the crossover line between the two tanks.")},
    "well": {"steel": ("pumpjack frame", "The pumpjack that lifts oil and water from the well."),
             "pipe": ("flowline", "Carries the well's water to its tank.")},
    "swd": {"steel": ("injection equipment", "Pumps and piping that inject the water underground.")},
    "truck": {"tank": ("cargo tank", "Holds the water the truck hauls."),
              "steel": ("cab and chassis", "The truck itself.")},
}

# Planned functional parts (docs/JARVIS_CONSOLE.md section 3), worded here so Jarvis can
# name them as soon as field.glb has them.
PLANNED = {
    "motor": ("electric motor", "Drives the pump or the pumpjack."),
    "casing": ("pump casing", "The housing the water is pushed through."),
    "coupling": ("coupling", "Joins the motor shaft to the pump shaft."),
    "skid": ("skid", "The steel base the pump and motor are bolted to."),
    "panel": ("control panel", "Local start and stop, and the run lamp."),
    "body": ("valve body", "The gate valve itself."),
    "bonnet": ("bonnet", "The cover over the valve's gate."),
    "stem": ("stem", "Rises and falls as the valve opens and closes."),
    "frame": ("pumpjack frame", "The pumpjack that lifts oil and water from the well."),
    "horsehead": ("horsehead", "The curved head that lifts the rod string."),
    "wellhead": ("wellhead", "The valves and fittings on top of the well."),
    "controller": ("pump-off controller", "Starts and stops the pumpjack."),
    "shell": ("tank shell", "The steel tank."),
    "stairs": ("stairs and railings", "The stairs and the walkway on top."),
    "hatch": ("thief hatch", "The hatch on the roof for gauging by hand."),
    "inlet": ("inlet", "Where the wells' water comes in."),
    "outlet": ("outlet", "Where the transfer pump draws the water out."),
    "cab": ("cab", "The truck's cab."),
    "cargo_tank": ("cargo tank", "Holds the water the truck hauls."),
    "hose": ("hose", "Hooks up to a tank's load-out stand."),
}

# The parts each kind has in field.glb today, then the planned ones, in display order.
PARTS = {
    "tank": ["tank", "steel", "LEVEL", "gauge", "lt", "lshh", "loadout", "label", "sensor", "rtu"],
    "pump": ["body", "steel", "rotor", "light", "sensor", "rtu"],
    "valve_d": ["steel", "wheel"],
    "valve_xo": ["steel", "wheel"],
    "well": ["steel", "beam", "crank", "pipe"],
    "swd": ["steel", "detail_tank", "tower", "lamps", "gate"],
    "truck": ["tank", "steel", "axle_1", "beacon", "headlights", "glass"],
}
PLANNED_PARTS = {
    "pump": ["motor", "casing", "coupling", "skid", "panel"],
    "valve_d": ["body", "bonnet", "stem"],
    "valve_xo": ["body", "bonnet", "stem"],
    "well": ["frame", "horsehead", "wellhead", "motor", "controller"],
    "tank": ["shell", "stairs", "hatch", "inlet", "outlet"],
    "truck": ["cab", "cargo_tank", "hose"],
}

# Which live signal a part shows: (kind, key) -> signal in GET /api/live/state.
SIGNAL = {
    ("tank", "LEVEL"): "level_pct", ("tank", "gauge"): "level_pct", ("tank", "lt"): "level_pct",
    ("tank", "sensor"): "level_pct",
    ("pump", "rotor"): "running", ("pump", "light"): "status", ("pump", "sensor"): "flow_bbl_hr",
    ("valve_d", "wheel"): "position", ("valve_xo", "wheel"): "position",
    ("well", "beam"): "flow_bbl_hr", ("well", "crank"): "flow_bbl_hr",
    ("swd", "lamps"): None, ("truck", "beacon"): "phase", ("truck", "axle_1"): "phase",
}


def part_kind(asset_id: str, kind: Optional[str]) -> Optional[str]:
    """fieldmap kind -> parts.js kind: valves split into valve_d and valve_xo."""
    if kind == "valve":
        return "valve_xo" if asset_id.startswith("VALVE_XO_") else "valve_d"
    return kind


def part_words(kind: str, key: str) -> dict:
    """parts.js partWords(kind, key): {label, what}."""
    for k, (label, what) in BY_KIND.get(kind, {}).items():
        if k == key or (k == "steel" and "steel" in key):
            return {"label": label, "what": what}
    for pattern, label, what in GENERIC:
        m = re.search(pattern, key)
        if m:
            return {"label": f"{label} (axle {m.group(1)})" if pattern.startswith("^axle") else label, "what": what}
    if key in PLANNED:
        label, what = PLANNED[key]
        return {"label": label, "what": what}
    return {"label": key.replace("_", " ").lower(), "what": ""}


def parts_for(kind: Optional[str], asset_id: str, live_entry: Optional[dict] = None,
              shutdown_pct: Optional[float] = None) -> dict:
    """Every part of one asset with its plain words, and the live value it shows."""
    pk = part_kind(asset_id, kind)
    live_entry = live_entry or {}
    out = []
    for key in PARTS.get(pk or "", []):
        words = part_words(pk, key)
        row = {"key": key, "label": words["label"], "what": words["what"]}
        sig = SIGNAL.get((pk, key))
        if sig:
            row["signal"] = sig
            row["value"] = live_entry.get(sig)
        if pk == "tank" and key == "lshh" and shutdown_pct is not None:
            row["what"] = f"{words['what']} Trips at {shutdown_pct:g}%. A switch, not wired to the console."
        out.append(row)
    for key in PLANNED_PARTS.get(pk or "", []):
        words = part_words(pk, key)
        out.append({"key": key, "label": words["label"], "what": words["what"], "planned": True})
    return {"asset_id": asset_id, "kind": pk, "parts": out}
