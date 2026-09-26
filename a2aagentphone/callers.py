"""Per-caller credentials: who is calling, not just whether the token matches.

Until now the ear compared the presented token against one string in a file.
That worked for the first pair and stopped working the moment there were two:
one token cannot be revoked for one caller without breaking the other, the log
can record an address but never a name, and "until when" and "from where" are
properties of the phone rather than of the caller.

This is the other half of the principle. All the policy is at the door — so the
door has to know who is standing in it.

## What is checked, in this order

1. **The hash matches a caller.** Only hashes are stored: a stolen copy of
   ``callers.db`` lets nobody call anybody.
2. **The row is not revoked.** Revoked rather than deleted, because *"who had
   access in March?"* is the question asked after a scare, and ``DELETE`` also
   erases the fact that the thing existed.
3. **It has not expired.**
4. **The address is allowed**, unless the caller is registered as
   ``0.0.0.0/0`` — which is a real answer, and has to be written out rather
   than left blank, so that "from anywhere" is a decision somebody made.

Every failure answers the same 401 with the same body. Telling a caller *which*
of the four it failed is telling an attacker which part of the credential it
got right.

## Two honest limits

**The address is only as good as what is in front.** Behind a reverse proxy the
socket peer is the proxy, so the check uses the forwarded address — and a
caller that already holds a valid token can put anything in that header unless
the proxy overwrites it. Treat the CIDR as a second lock on a stolen token, not
as proof of origin.

**``scope`` is recorded and not enforced.** It says what a caller was given the
number for; it does not cap what the agent may do, because capping the agent is
the blunt control this design rejects. A caller who should not be able to do
something needs a phone answered by a user who cannot do it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def address_allowed(address: str | None, allowed_from: str) -> bool:
    """Is this address inside any of the caller's CIDRs?"""
    nets = [n.strip() for n in (allowed_from or "").split(",") if n.strip()]
    if not nets:
        return False
    if any(n in ("0.0.0.0/0", "::/0") for n in nets):
        return True
    if not address:
        # A caller pinned to a range, arriving from an address we cannot see,
        # fails closed. The alternative is a pin that stops meaning anything
        # the moment something in the path hides the origin.
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # An IPv4 connection on a dual-stack box can arrive as ``::ffff:192.0.2.10``,
    # which read literally is an IPv6 address and does NOT match 192.0.2.10/32
    # -- the same machine, refused. DESIGN.md has listed this as a known trap
    # since the start, described as "one line of code, and an hour of confusion
    # if forgotten", and then it was forgotten until 2026-09-22.
    #
    # What makes it expensive is how it shows up: the caller is correctly
    # registered, holds the right token, and gets a 401 indistinguishable from
    # an unknown, revoked or expired one -- because those are deliberately
    # indistinguishable. There is no thread to pull.
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    for net in nets:
        try:
            if ip in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def count(db: str | Path) -> int:
    """How many callers are admitted. Zero is a phone that refuses every call.

    Used for the startup banner only. It used to gate the fallback to the old
    single-token file, and *that* is what made revoking the last caller reopen
    the shared token instead of closing the line. Nothing in the request path
    asks this question any more: :func:`identify` either finds a row or does
    not.
    """
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            return c.execute(
                "SELECT COUNT(*) FROM callers WHERE revoked_at IS NULL"
            ).fetchone()[0]
    except sqlite3.Error:
        return 0


def open_origin(db: str | Path) -> list[str]:
    """Admitted callers that may ring from anywhere.

    Said out loud at every startup, because ``0.0.0.0/0`` is a legitimate
    answer and an unconsidered one look exactly alike in the table. The banner
    is the one place the difference can still be noticed.
    """
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            rows = c.execute(
                "SELECT alias, allowed_from FROM callers WHERE revoked_at IS NULL"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        alias
        for alias, allowed in rows
        if any(n.strip() in ("0.0.0.0/0", "::/0") for n in (allowed or "").split(","))
    ]


def identify(db: str | Path, token: bytes | str, address: str | None) -> dict | None:
    """Return the caller this token belongs to, or None.

    None covers every reason equally -- unknown, revoked, expired, wrong
    address -- because the caller is told the same thing in every case.
    """
    if isinstance(token, bytes):
        token = token.decode("utf-8", "replace")
    digest = token_hash(token)
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM callers WHERE token_hash=?", (digest,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row["revoked_at"]:
        return None
    expires = _parse(row["expires_at"])
    if expires is not None and _now() >= expires:
        return None
    if not address_allowed(address, row["allowed_from"]):
        return None
    return dict(row)
