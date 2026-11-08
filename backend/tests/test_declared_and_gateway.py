"""Final-round review of #57: declared-but-silent signals go stale; a gateway 5xx is UNCERTAIN."""
import httpx
import pytest

from backend.tests.test_live import LiveField, healthy_scenario, run, tick  # noqa: F401


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HOOK_TOKEN", "s3cret")
    for v in ("ANTHROPIC_API_KEY", "JARVIS_MODE", "JARVIS_CONFIRM"):
        monkeypatch.delenv(v, raising=False)


def hook_field():
    raw = {"connectors": [{"id": "CONN_HOOK", "kind": "webhook",
                           "config": {"url": "http://gateway.local/cmd", "token_env": "HOOK_TOKEN"},
                           "signals": [{"asset_id": "PUMP_2", "signal": "running", "address": "p2/run"}],
                           "commands": [{"asset_id": "VALVE_XO_34", "command": "SET_VALVE", "address": "xo34/cmd"}]}]}
    return LiveField(healthy_scenario(), raw)


def test_declared_signal_that_never_reports_goes_stale_after_the_grace():
    live = hook_field()
    tick(live, 4)                        # 1 live minute: still inside the grace
    assert not any(a["asset_id"] == "PUMP_2" and a["kind"] == "SENSOR_STALE" for a in live.detector.active.values())
    tick(live, 4 * 6)                    # past stale_after_min (5) with no PUMP_2 reading ever
    stale = [a for a in live.detector.active.values() if a["asset_id"] == "PUMP_2" and a["kind"] == "SENSOR_STALE"]
    assert stale, "a declared signal that never reported must raise SENSOR_STALE"
    assert live.state()["assets"]["PUMP_2"]["status"] != "OK"


@pytest.mark.parametrize("code,uncertain", [(502, True), (504, True), (500, True), (408, True), (404, False), (400, False)])
def test_gateway_errors_are_uncertain_client_errors_are_failed(code, uncertain):
    live = hook_field()
    hook = live.registry.get("CONN_HOOK")
    sent = []
    hook.transport = httpx.MockTransport(lambda req: (sent.append(1), httpx.Response(code))[1])
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}))
    out = run(live.confirm(p["id"]))
    assert len(sent) == 1                                   # sent exactly once, never retried
    assert out["status"] == ("UNCERTAIN" if uncertain else "FAILED"), out
