"""Turn raw urllib3/requests transport errors into something a user can act on.

dCloud, CAI, and CAMGR are internal-only hosts, so the usual failure is simply
being off the Cisco network. Left alone, requests surfaces that as a wall of
NameResolutionError / Max retries exceeded text.
"""

from __future__ import annotations

import socket
import threading
import time

OFF_NETWORK_HINT = (
    "Not on the Cisco network. Connect to the Cisco VPN (or an office network), "
    "then try again."
)

# Lowercased fragments urllib3 / requests use when DNS or the TCP connect fails.
_OFF_NETWORK_MARKERS = (
    "nameresolutionerror",
    "failed to resolve",
    "nodename nor servname",
    "name or service not known",
    "temporary failure in name resolution",
    "getaddrinfo failed",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "connectionerror",
    "connecttimeout",
    "max retries exceeded",
)


def looks_off_network(err: object) -> bool:
    text = str(err or "").lower()
    return any(marker in text for marker in _OFF_NETWORK_MARKERS)


def describe_request_error(err: object, service: str = "") -> str:
    """Friendly message for a transport error, or the original text if it is useful."""
    if not looks_off_network(err):
        return str(err or "").strip()
    return off_network_message(service)


def off_network_message(service: str = "") -> str:
    name = (service or "").strip()
    target = f"Could not reach {name}." if name else "Could not reach the server."
    return f"{target} {OFF_NETWORK_HINT}"


_RESOLVE_TTL_SECONDS = 15.0
_resolve_cache: dict[str, tuple[float, bool]] = {}
_resolve_lock = threading.Lock()


def host_resolves(host: str) -> bool:
    """DNS-only check. Internal Cisco hosts do not resolve off the VPN.

    Cached briefly: status polling and the auth keepalive both call this, and a
    failed lookup can sit on the resolver timeout.
    """
    name = str(host or "").strip()
    if not name:
        return True
    now = time.monotonic()
    with _resolve_lock:
        cached = _resolve_cache.get(name)
        if cached and now - cached[0] < _RESOLVE_TTL_SECONDS:
            return cached[1]
    try:
        socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
        ok = True
    except OSError:
        ok = False
    with _resolve_lock:
        _resolve_cache[name] = (time.monotonic(), ok)
    return ok
