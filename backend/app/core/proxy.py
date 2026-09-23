"""Client-IP resolution behind trusted proxies — ARCHITECTURE.md §21.7.1.

1. If the immediate peer is NOT in TRUSTED_PROXY_CIDRS, the peer is the
   client and every forwarding header is ignored.
2. If it is trusted, walk X-Forwarded-For from the RIGHT, discarding
   entries that are themselves trusted proxies; the first untrusted
   address is the client.
3. TRUSTED_PROXY_CIDRS defaults to empty — trust nothing.

Taking the leftmost X-Forwarded-For entry is the spoofable choice and
is prohibited: it is whatever the client wrote. Pure functions only, so
every rule above is unit-testable without a request.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_trusted_cidrs(raw: str) -> tuple[Network, ...]:
    """Comma-separated CIDRs; empty means trust nothing. Raises
    ValueError on anything malformed, so a typo fails at startup rather
    than silently trusting nobody (or everybody)."""
    return tuple(
        ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()
    )


def _is_trusted(address: str, trusted: Sequence[Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in trusted)


def resolve_client_ip(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]
) -> str:
    """The address rate limits key on (§21.7's "shared-IP ceiling")."""
    peer_address = peer or "unknown"
    if not _is_trusted(peer_address, trusted):
        return peer_address

    hops = [hop.strip() for hop in (forwarded_for or "").split(",") if hop.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            # A trusted proxy appended something that is not an address.
            # Walking further left would reach client-written entries,
            # so fail safe onto the proxy's own address instead.
            return peer_address
        if not _is_trusted(hop, trusted):
            return hop
    # Every hop was a trusted proxy (or there were none): the nearest
    # known address is the peer itself.
    return peer_address
