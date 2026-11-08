"""Regression (review of #74 at 7efca87): 20 PUBACK timeouts must not use up paho's in-flight slots."""
import time

from backend.tests.test_connectors_real import (blueprint, conn_status, field, live_factory,  # noqa: F401
                                                no_llm, plc, scada, wait)


def test_commands_still_go_out_after_20_puback_timeouts(field, scada, plc, live_factory):
    raw = blueprint(scada.port, plc.port)
    raw["connectors"][0]["config"]["timeout_s"] = 0.15
    live = live_factory(raw)
    conn = live.registry.get("CONN_SCADA")
    wait(live, lambda: conn_status(live)["CONN_SCADA"] == "connected", scada)
    cmd = {"type": "SET_PUMP", "asset_id": "PUMP_2", "value": "OFF"}
    scada.broker.drop_pubacks = True
    for _ in range(20):
        assert conn.send(cmd).get("uncertain")
    time.sleep(0.3)
    before = len(scada.commands)
    scada.broker.drop_pubacks = False
    conn.timeout = 2
    r = conn.send(cmd)
    time.sleep(0.5)
    assert r["ok"] and not r.get("uncertain"), r       # CONFIRMED, not a false "sent"
    assert len(scada.commands) - before == 1            # delivered exactly once
