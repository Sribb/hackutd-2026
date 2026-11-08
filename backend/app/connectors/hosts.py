"""Which hosts a field connector may reach (#126 verification round 1).

An uploaded blueprint names the hosts its connectors connect to, and a connector starts
connecting as soon as the blueprint is activated. Without a limit, anyone who can upload
a blueprint could point the backend at any address it can reach (a cloud metadata
endpoint, another service on the LAN) and read back from the connector status whether
it answered. So a connector host has to be on an operator's list:

    FREEBOARD_CONN_ALLOWED_HOSTS="127.0.0.1,localhost,10.0.0.0/24,scada.plant.local,.plant.local"

Each entry is an IP address, a network in CIDR form (matches IP addresses only), a host
name (exact match, any case), or a name starting with "." (that domain and every name
under it). "*" allows any host except link-local addresses, cloud metadata addresses
and names, which must be listed on their own. Names are compared as written; nothing is
resolved, so a check never waits on DNS.

A host is read the way the operating system's resolver reads it before anything is
compared (#126 verification round 2). The resolver takes an IPv4 address in more forms
than dotted decimal: one number (2852039166), hex (0xa9fea9fe), octal (0251.0376.0251.0376),
or fewer than four parts (169.254.43518). Each of those is turned into its dotted
address first, so 2852039166 is 169.254.169.254 and is refused like it. A trailing dot
is dropped. An IPv6 address that carries an IPv4 one (IPv4-mapped, IPv4-compatible,
NAT64, 6to4, Teredo) is checked as that IPv4 address too.

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


# Cloud metadata services that are not link-local: AWS over IPv6, Alibaba Cloud, and
# Azure's wire server.
METADATA_IPS = frozenset(ipaddress.ip_address(a) for a in ("fd00:ec2::254", "100.100.100.200", "168.63.129.16"))
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _inet_aton(text: str):
    """The IPv4 address the classic resolver (inet_aton) reads `text` as, or None: one to
    four parts, each decimal, octal (leading 0) or hex (leading 0x); the last part fills
    the bytes the others leave."""
    parts = text.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    nums = []
    for part in parts:
        p = part.lower()
        try:
            if p.startswith("0x"):
                n = int(p[2:], 16) if len(p) > 2 else 0
            elif len(p) > 1 and p.startswith("0"):
                n = int(p[1:], 8)
            elif p.isdigit():
                n = int(p)
            else:
                return None
        except ValueError:
            return None
        nums.append(n)
    *head, last = nums
    if any(n > 255 for n in head) or last >= 256 ** (4 - len(head)):
        return None
    value = 0
    for n in head:
        value = value * 256 + n
    value = value * 256 ** (4 - len(head)) + last
    return ipaddress.IPv4Address(value)


def _ip(text: str):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    if text and text.replace(".", "").replace("x", "").replace("X", "").isalnum() and text[0].isdigit():
        return _inet_aton(text)
    return None


def _canonical(host: str) -> str:
    """The host as compared here: lower case, no brackets, no trailing dot, and an IP
    address in its standard form however the resolver would have read it."""
    host = host.strip().lower().strip("[]").rstrip(".")
    ip = _ip(host)
    return str(ip) if ip is not None else host


def _embedded_v4(ip):
    """The IPv4 address an IPv6 address carries, or None."""
    if ip.version != 6:
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    packed = ip.packed
    if ip in NAT64 or (packed[:12] == bytes(12) and packed[12:] != bytes(4) and int(ip) > 1):
        return ipaddress.IPv4Address(packed[12:])   # NAT64, or IPv4-compatible (::a.b.c.d)
    return None


def _entries() -> list[str]:
    raw = os.environ.get(ALLOWED_ENV)
    if raw is None or not raw.strip():
        return list(DEFAULT)
    out = []
    for e in raw.replace(";", ",").replace(" ", ",").split(","):
        e = e.strip().lower().strip("[]")
        if e:
            out.append(e if e == "*" or "/" in e or e.startswith(".") else _canonical(e))
    return out


def _sensitive(host: str) -> bool:
    ip = _ip(host)
    if ip is not None:
        for a in (ip, _embedded_v4(ip)):
            if a is not None and (a.is_link_local or a in METADATA_IPS):
                return True
        return False
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
    host = _canonical(host)
    if not host:
        return False
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
