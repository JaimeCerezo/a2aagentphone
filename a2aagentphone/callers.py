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
   ``phone.db`` lets nobody call anybody.
2. **The row is not revoked.** Revoked rather than deleted, because *"who had
   access in March?"* is the question asked after a scare, and ``DELETE`` also
   erases the fact that the thing existed.
3. **It has not expired.**

Every failure answers the same 401 with the same body. Telling a caller *which*
of the three it failed is telling an attacker which part of the credential it
got right.

## There is no fourth check, since v0.5.0

There used to be one: ``allowed_from``, a list of CIDRs the credential could be
used from. **Removed on purpose, because in this fleet it could not be true.**

Two things in the normal path rewrite the origin before the ear ever sees it:

* **An agent inside a container calling its own host** arrives with the source
  rewritten by SNAT to the bridge gateway — not the container's address, and
  not the machine's either. The pin has to be written against an address that
  belongs to nobody.
* **The reverse proxy in front** decides what ``X-Forwarded-For`` says. Traefik
  can be configured to trust, rewrite or discard it, and the value the ear
  reads is whatever that configuration left behind.

So the field named the caller's address and held the proxy's opinion of it. A
control that is right only when nothing in the path touches the packet is not a
control; it is a field that reads like one, which is worse than nothing,
because it is what an audit sees and believes.

**The address is still recorded** — ``calls.remote_addr``, with the socket peer
kept alongside it in ``calls.via`` when the two differ. Evidence about where a
call seemed to come from is worth keeping. Evidence is not the same as a lock,
and the mistake was letting one be spelled like the other.

## Two honest limits

**A token is now the whole credential.** Whoever holds it, calls. There is no
second factor left on this row, so the defence that matters is the one on the
*other* side of the door: what the answering user can do. See DESIGN.md §6.

**``scope`` is recorded and not enforced.** It says what a caller was given the
number for; it does not cap what the agent may do, because capping the agent is
the blunt control this design rejects. A caller who should not be able to do
something needs a phone answered by a user who cannot do it.
"""

from __future__ import annotations

import hashlib
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


def identify(db: str | Path, token: bytes | str) -> dict | None:
    """Return the caller this token belongs to, or None.

    None covers every reason equally -- unknown, revoked, expired -- because
    the caller is told the same thing in every case.

    **No address is passed in any more.** It used to be the fourth argument and
    the fourth check; see the module docstring for why a pin that the network
    path rewrites was removed rather than documented around.
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
    return dict(row)
