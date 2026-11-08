"""Pin the tests to the backend's own placeholder scenario, so a change in
scenario/field.json can't break them. test_team_scenario.py covers that file."""
import os
from pathlib import Path

import pytest

os.environ["FREEBOARD_SCENARIO"] = str(Path(__file__).resolve().parents[1] / "data" / "scenario.json")
# The legacy API tests check the forecast and planner math against that fixed scenario, so
# they run on the static clock. test_one_clock.py turns the live clock on (#42).
os.environ.setdefault("FREEBOARD_ONE_CLOCK", "0")


@pytest.fixture(autouse=True)
def _fresh_auth_throttle():
    """Every test starts with no wrong-token count (auth.THROTTLE is process-wide)."""
    from backend.app import auth
    auth.THROTTLE.reset()
    yield
    auth.THROTTLE.reset()
