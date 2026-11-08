"""Dataset mode: the backend runs on the EOG "Beat the Shutdown" sponsor data.

A dataset is a folder with network.json, ops.json and telemetry.csv
(https://github.com/nihaal419/HackUTD-ZeroDay, SPEC.md). Point FREEBOARD_EOG_DIR at a checkout
of that repo (or any folder of datasets) and every folder holding the three files becomes
selectable, including the `test/` folder the organizers add before judging. Nothing about a
particular network is hardcoded: systems, facilities, prices, trucks and limits all come from
the files, and "now" is the last telemetry timestamp, never the system clock.
"""
