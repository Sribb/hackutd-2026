"""Registers the field connectors built for #126 into the connector registry's KINDS.

main.py imports this module once, before the live layer is installed, so a blueprint's
`connectors` list can use these kinds:

    opcua       OpcUaConnector (opcua_ua.py), replacing the opcua stub
    sparkplug   SparkplugConnector (sparkplug.py)

Each connector imports its protocol library only when it is built, through
signalmap.import_optional, so a missing library makes that connector show unavailable
with the pip hint. If the connector's own module fails to import, its kind is still
registered, as a connector that is always unavailable and says why, so the backend
starts either way and never raises ImportError here.
"""
from __future__ import annotations

import importlib
import logging
from typing import Callable

from . import KINDS
from .base import UnavailableConnector

log = logging.getLogger("freeboard.connectors")

# kind -> (module in this package, class name, pip package for the hint)
LANDED = {
    "opcua": ("opcua_ua", "OpcUaConnector", "asyncua"),
    "sparkplug": ("sparkplug", "SparkplugConnector", "paho-mqtt"),
}
FAILED: dict[str, str] = {}   # kind -> why its module didn't import


def _unavailable(kind: str, why: str) -> type:
    """A connector class for `kind` that builds fine and always reports `why`."""

    class Missing(UnavailableConnector):
        def __init__(self, spec: dict, fmap, now: Callable[[], float]) -> None:
            super().__init__(spec, fmap, now, why)
            self.kind = kind

    Missing.__name__ = f"Unavailable_{kind}"
    return Missing


def register() -> dict[str, str]:
    """Put every landed kind into KINDS. Returns {kind: why} for those that failed."""
    for kind, (module, cls_name, package) in LANDED.items():
        try:
            mod = importlib.import_module(f".{module}", __package__)
            KINDS[kind] = getattr(mod, cls_name)
            FAILED.pop(kind, None)
        except Exception as e:  # noqa: BLE001 - ImportError or a broken install: never stop startup
            why = (f"the {kind} connector could not be loaded "
                   f"(pip install -r backend/requirements-connectors.txt, or pip install {package}): "
                   f"{type(e).__name__}: {e}")
            FAILED[kind] = why
            KINDS[kind] = _unavailable(kind, why)
            log.warning("%s", why)
    return dict(FAILED)


register()
