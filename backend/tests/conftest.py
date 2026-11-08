"""Pin the tests to the backend's own placeholder scenario, so a change in
scenario/field.json can't break them. test_team_scenario.py covers that file."""
import os
from pathlib import Path

os.environ["FREEBOARD_SCENARIO"] = str(Path(__file__).resolve().parents[1] / "data" / "scenario.json")
# The legacy API tests check the forecast and planner math against that fixed scenario, so
# they run on the static clock. test_one_clock.py turns the live clock on (#42).
os.environ.setdefault("FREEBOARD_ONE_CLOCK", "0")
