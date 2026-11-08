"""Actuators: what carries out an approved command.

Only the simulated actuator exists. It waits a realistic delay and then ACKs, and
the command bus records the action on the active field. Real protocol writes
(MQTT, Modbus, OPC UA) are out of scope (README: no real field equipment). They
would also need ALLOW_ACTUATION=1 plus an allowlist for each asset, and this
module refuses them regardless.

SIM_ACK_SCALE scales the delays (default 0.05, so a 20 s manual valve ACKs in
about 1 s for the demo). Set it to 1 for real time, or 0 for tests.
"""
from __future__ import annotations

import os
import time

DELAY_S = {"SET_VALVE": 20.0, "SET_PUMP": 5.0, "CHOKE_WELL": 3.0, "SEND_TRUCK": 2.0, "RESET_PUMP": 30.0}


def _scale() -> float:
    try:
        return max(0.0, float(os.environ.get("SIM_ACK_SCALE", "0.05")))
    except ValueError:
        return 0.05


def simulated(action: dict) -> tuple[bool, str]:
    delay = DELAY_S.get(action.get("type"), 2.0) * _scale()
    if delay:
        time.sleep(delay)
    return True, f"Simulated {action.get('type')} on {action.get('asset_id')} acknowledged."


def real(_action: dict) -> tuple[bool, str]:
    return False, "Real equipment writes are disabled in this build."


def get():
    if os.environ.get("ACTUATOR", "sim") != "sim":
        return real
    return simulated
