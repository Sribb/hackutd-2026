"""Fake field equipment for the connector tests and the local demo.

Every fake listens on 127.0.0.1 only and runs as `python -m backend.fakes.<name>`, which
prints where it listens. Tests start one in-process with its start() function, which
returns a handle with stop().
"""
