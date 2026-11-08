"""Pin the tests to the backend's own placeholder scenario, so a change in
scenario/field.json can't break them. test_team_scenario.py covers that file."""
import os
from pathlib import Path

import pytest

os.environ["FREEBOARD_SCENARIO"] = str(Path(__file__).resolve().parents[1] / "data" / "scenario.json")


@pytest.fixture(autouse=True)
def _fresh_auth_throttle():
    """Every test starts with no wrong-token count (auth.THROTTLE is process-wide)."""
    from backend.app import auth
    auth.THROTTLE.reset()
    yield
    auth.THROTTLE.reset()
