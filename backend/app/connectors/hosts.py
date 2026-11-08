"""Which hosts a field connector may reach (#126 verification round 1).

An uploaded blueprint names the hosts its connectors connect to, and a connector starts
connecting as soon as the blueprint is activated. Without a limit, anyone who can upload
a blueprint could point the backend at any address it can reach (a cloud metadata
endpoint, another service on the LAN) and read back from the connector status whether
it answered. So a connector host has to be on an operator's list:

    FREEBOARD_CONN_ALLOWED_HOSTS="127.0.0.1,localhost,10.0.0.0/24,scada.plant.local,.plant.local"

Each entry is an IP address, a network in CIDR form (matches IP addresses only), a host
name (exact match, any case), or a name starting with "." (that domain and every name
under it). "*" allows any host except link-local addresses and cloud metadata names,
which must be listed on their own. Names are compared as written; nothing is resolved,
so a check never waits on DNS.

When the variable is not set, only this machine is allowed (127.0.0.1, ::1, localhost),
which is what the demo fakes need. A host that is not allowed raises ValueError when the
connector is built, so the registry shows the connector unavailable with the reason.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Optional
from urllib.parse import urlsplit

ALLOWED_ENV = "FREEBOARD_CONN_ALLOWED_HOSTS"
DEFAULT = ("127.0.0.1", "::1", "localhost")
METADATA_NAMES = ("metadata", "metadata.internal", "metadata.google.internal", "instance-data",
                  "instance-data.ec2.internal")


def _ip(text: str):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _entries() -> list[str]:
    raw = os.environ.get(ALLOWED_ENV)
    if raw is None or not raw.strip():
        return list(DEFAULT)
    return [e.strip().lower().strip("[]") for e in raw.replace(";", ",").replace(" ", ",").split(",") if e.strip()]


def _sensitive(host: str) -> bool:
    ip = _ip(host)
    if ip is not None:
        return ip.is_link_local or (ip.version == 6 and ip.ipv4_mapped is not None and ip.ipv4_mapped.is_link_local)
    return host in METADATA_NAMES or host.endswith(".internal")


def _listed(host: str, entry: str) -> bool:
    ip = _ip(host)
    if "/" in entry:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return False
        return ip is not None and ip.version == net.version and ip in net
    if _ip(entry) is not None:
        return ip is not None and ip == _ip(entry)
    if entry.startswith("."):
        return ip is None and (host == entry[1:] or host.endswith(entry))
    return host == entry


def allowed(host: str) -> bool:
    """True when the operator's list lets a connector reach this host."""
    if not isinstance(host, str) or not host.strip():
        return False
    host = host.strip().lower().strip("[]")
    entries = _entries()
    if any(e != "*" and _listed(host, e) for e in entries):
        return True
    return "*" in entries and not _sensitive(host)


def check_host(host: str, what: str = "config host") -> None:
    """Raise ValueError unless a connector may reach `host`."""
    if not allowed(host):
        listed = "it is not set, so only this machine is allowed" if not os.environ.get(ALLOWED_ENV, "").strip() \
            else "the host is not on it"
        raise ValueError(f"{what} {host!r} is not allowed: an operator lists the hosts connectors may reach in "
                         f"{ALLOWED_ENV}, and {listed}")


def check_url(url: Optional[str], what: str = "config url", schemes: tuple = ("http", "https")) -> None:
    """Raise ValueError unless `url` uses one of `schemes`, carries no credentials, and
    names a host a connector may reach."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{what} must be a URL")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in schemes:
        raise ValueError(f"{what} must start with {' or '.join(s + '://' for s in schemes)}")
    if "@" in parts.netloc:
        raise ValueError(f"{what} must not carry credentials; name an environment variable instead")
    if not parts.hostname:
        raise ValueError(f"{what} has no host")
    check_host(parts.hostname, what + " host")
