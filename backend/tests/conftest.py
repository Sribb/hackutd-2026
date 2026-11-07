"""Pin the tests to the backend's own placeholder scenario, so a change in
scenario/field.json can't break them. test_team_scenario.py covers that file."""
import os
from pathlib import Path

os.environ["FREEBOARD_SCENARIO"] = str(Path(__file__).resolve().parents[1] / "data" / "scenario.json")
