"""``a2aagentphone-admin`` — the only supported way to edit a phone's lists.

Two lists, kept apart because their owners are:

* **callers** — who may ring this phone. Hashes only.
* **contacts** — who this phone may ring. Usable tokens.

Why a command rather than editing a file: every row has fields that must be
decided rather than defaulted, and a file lets you leave them out. ``allowed_from``
is the example that matters — leaving it blank would quietly mean "anywhere",
so here it has to be typed, and "anywhere" is spelled ``0.0.0.0/0`` in full.

A token is shown **once**, when it is minted, and never again: only its hash is
kept. Carry it by hand from there. Not through a chat with an agent, not
through a repository -- that moment is the only time the string exists outside
a protected database, and a transcript keeps everything forever.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import callers as callers_mod
from . import db as db_mod


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"a2aagentphone-admin: no database at {path}. Run install.sh first.")
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


def _dir(args) -> Path:
    d = Path(args.db)
    if not d.is_dir():
        sys.exit(f"a2aagentphone-admin: {d} is not a phone directory.")
    return d


# --------------------------------------------------------------------------
# callers: who may ring this phone
# --------------------------------------------------------------------------
def _mint(path: Path, alias: str) -> str:
    """Make room for a new credential under an alias that may already exist.

    ``alias`` is the primary key and revoking keeps the row, so the obvious
    flow -- revoke, then register again -- failed with a constraint error the
    first time it was tried for real. Rotation is not an edge case; it is the
    normal life of a credential.

    The old row is kept under a stamped alias instead of being deleted, so
    *"who had access in March?"* still has an answer, and the name is free
    again.
    """
    with _conn(path) as c:
        row = c.execute("SELECT * FROM callers WHERE alias=?", (alias,)).fetchone()
        if row is not None:
            if not row["revoked_at"]:
                sys.exit(
                    f"a2aagentphone-admin: «{alias}» is active. Use `caller rotate "
                    f"{alias}` to replace its token, or `caller revoke {alias}` "
                    "first if you mean to take the access away."
                )
            stamp = row["revoked_at"].replace(":", "").replace("-", "")[:15]
            c.execute(
                "UPDATE callers SET alias=? WHERE alias=?",
                (f"{alias}@revoked-{stamp}", alias),
            )
    return secrets.token_urlsafe(32)


def caller_rotate(args) -> None:
    """New token, same caller. Everything else about the row stays put."""
    path = db_mod.path_for(_dir(args))
    token = secrets.token_urlsafe(32)
    with _conn(path) as c:
        n = c.execute(
            "UPDATE callers SET token_hash=? WHERE alias=? AND revoked_at IS NULL",
            (callers_mod.token_hash(token), args.alias),
        ).rowcount
    if not n:
        sys.exit(f"a2aagentphone-admin: no active caller «{args.alias}».")
    print(f"«{args.alias}» rotated. The previous token stops working now.")
    print()
    print(f"  token:   {token}")
    print()
    print("  Shown once. Carry it by hand; tell an agent the PATH, not the value.")


def caller_add(args) -> None:
    path = db_mod.path_for(_dir(args))
    token = _mint(path, args.alias)
    expires = (
        (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat(
            timespec="seconds"
        )
        if args.days
        else None
    )
    with _conn(path) as c:
        try:
            c.execute(
                "INSERT INTO callers (alias, token_hash, auth, allowed_from,"
                " scope, expires_at, note, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    args.alias,
                    callers_mod.token_hash(token),
                    args.auth,
                    args.allowed_from,
                    args.scope,
                    expires,
                    args.note,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as e:
            sys.exit(f"a2aagentphone-admin: {e}. Is '{args.alias}' already registered?")

    print(f"Caller «{args.alias}» added.")
    print(f"  from:    {args.allowed_from}")
    print(f"  auth:    {args.auth}")
    print(f"  scope:   {args.scope}")
    print(f"  expires: {expires or 'never'}")
    print()
    print(f"  token:   {token}")
    print()
    print("  This token will NOT be shown again -- only its hash is stored.")
    print("  Carry it by hand. If the other end is an agent, write it to a file")
    print("  and tell them the PATH; a token pasted into a conversation stays")
    print("  in that conversation for good.")


def caller_list(args) -> None:
    with _conn(db_mod.path_for(_dir(args))) as c:
        rows = c.execute("SELECT * FROM callers ORDER BY created_at").fetchall()
    if not rows:
        print("no callers registered -- this phone still uses its single token file.")
        return
    for r in rows:
        state = "REVOKED" if r["revoked_at"] else (r["expires_at"] or "never")
        print(f"{r['alias']:20} {r['auth']:12} {r['allowed_from']:20} {state}")
        if r["note"]:
            print(f"  {r['note']}")


def caller_revoke(args) -> None:
    with _conn(db_mod.path_for(_dir(args))) as c:
        n = c.execute(
            "UPDATE callers SET revoked_at=? WHERE alias=? AND revoked_at IS NULL",
            (_now(), args.alias),
        ).rowcount
    # Revoked, not deleted: the row is the answer to "who had access in March?"
    print(f"«{args.alias}» revoked." if n else f"no active caller «{args.alias}».")


# --------------------------------------------------------------------------
# contacts: who this phone may ring
# --------------------------------------------------------------------------
def contact_add(args) -> None:
    path = db_mod.path_for(_dir(args))
    token = args.token
    if token == "-":
        # Read from stdin, never from an argument: an argument is visible in
        # `ps` for as long as the process lives, and in the shell history
        # afterwards.
        token = sys.stdin.read().strip()
    elif args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    with _conn(path) as c:
        c.execute(
            "INSERT OR REPLACE INTO contacts (alias, url, token, expires_at,"
            " note, created_at) VALUES (?,?,?,?,?,?)",
            (args.alias, args.url, token, args.expires, args.note, _now()),
        )
    print(f"Contact «{args.alias}» -> {args.url}")
    print(f"  credential: {'yes' if token else 'NONE YET'}")


def contact_list(args) -> None:
    with _conn(db_mod.path_for(_dir(args))) as c:
        rows = c.execute("SELECT * FROM contacts ORDER BY alias").fetchall()
    if not rows:
        print("no contacts -- this phone cannot call anyone.")
        return
    for r in rows:
        print(
            f"{r['alias']:20} {r['url']:45} "
            f"{'token' if r['token'] else 'NO TOKEN':10} {r['expires_at'] or 'never'}"
        )


def contact_config(args) -> None:
    """Print, or install, the MCP entry that lets an agent use this phone.

    The gap this closes, reported by scm-intranet on 2026-09-22: every update
    rewrites the unit file and the environment file, and **none of them has
    ever touched the user's own MCP config**. So the incoming side of a machine
    moved to the database while the outgoing side stayed on the old
    environment-variable shape, and the two halves of the same phone ended up
    on different mechanisms. It looked, correctly, like having two address
    books.

    There is nothing to hand-edit now: one entry, pointing at the phone's
    directory, and the contacts come from the table.
    """
    directory = _dir(args)
    with _conn(db_mod.path_for(directory)) as c:
        rows = c.execute("SELECT alias FROM contacts ORDER BY alias").fetchall()
    entry = {
        # The stable path, never the venv: an install that moves would
        # otherwise break this, and not at startup -- on the next call, which
        # looks exactly like the other agent not answering.
        "command": "/usr/local/bin/a2aagentphone-mcp",
        "env": {"A2A_DB": str(directory), "A2A_TIMEOUT": "420"},
    }

    if not args.write:
        print(json.dumps({"mcpServers": {"a2aagentphone": entry}}, indent=2))
        if rows:
            print(f"\n# reachable: {', '.join(r['alias'] for r in rows)}")
        return

    target = Path(args.write).expanduser()
    config = {}
    if target.exists():
        try:
            config = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.exit(f"a2aagentphone-admin: {target} is not valid JSON; not touching it.")
        # A backup before rewriting somebody's config, because this file holds
        # far more than our entry and a bad merge would be expensive.
        backup = target.with_suffix(target.suffix + ".before-a2aagentphone")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"backed up to {backup}")

    servers = config.setdefault("mcpServers", {})
    before = servers.get("a2aagentphone")
    servers["a2aagentphone"] = entry
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    if before == entry:
        print(f"{target}: already pointing at {directory}")
    else:
        print(f"{target}: a2aagentphone now reads its contacts from {directory}")
        if before and "A2A_URL" in (before.get("env") or {}):
            print("  (replaced the old fixed-destination entry)")
    print(f"  reachable: {', '.join(r['alias'] for r in rows) or 'nobody yet'}")
    print("  The agent must be restarted for this to take effect.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="a2aagentphone-admin",
        description="Edit a phone's caller and contact lists.",
    )
    ap.add_argument(
        "--db",
        required=True,
        metavar="DIR",
        help="The phone's directory, e.g. /var/lib/a2aagentphone/<name>",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("caller", help="who may ring this phone").add_subparsers(
        dest="sub", required=True
    )
    a = c.add_parser("add", help="register a caller and mint its token")
    a.add_argument("alias")
    a.add_argument(
        "--from",
        dest="allowed_from",
        required=True,
        help="CIDRs this credential may be used from, comma separated. "
        "Required on purpose: '0.0.0.0/0' is a valid answer but has to be "
        "written out, so that 'from anywhere' is a decision and not a blank.",
    )
    a.add_argument("--scope", default="full",
                   help="What this caller was given the number for. Recorded, "
                        "not enforced: it does not cap what the agent may do.")
    a.add_argument("--auth", default="token", choices=["token", "token+mtls"])
    a.add_argument("--days", type=int, default=365, help="0 for no expiry.")
    a.add_argument("--note", default=None)
    a.set_defaults(func=caller_add)

    a = c.add_parser("list", help="show registered callers")
    a.set_defaults(func=caller_list)

    a = c.add_parser("revoke", help="revoke a caller, keeping the record")
    a.add_argument("alias")
    a.set_defaults(func=caller_revoke)

    a = c.add_parser("rotate", help="mint a new token for an existing caller")
    a.add_argument("alias")
    a.set_defaults(func=caller_rotate)

    t = sub.add_parser("contact", help="who this phone may ring").add_subparsers(
        dest="sub", required=True
    )
    a = t.add_parser("add", help="add or replace a contact")
    a.add_argument("alias")
    a.add_argument("--url", required=True)
    a.add_argument("--token", default="", help="'-' to read it from stdin.")
    a.add_argument("--token-file", default=None)
    a.add_argument("--expires", default=None)
    a.add_argument("--note", default=None)
    a.set_defaults(func=contact_add)

    a = t.add_parser("list", help="show contacts")
    a.set_defaults(func=contact_list)

    a = t.add_parser("config", help="print or install the agent's MCP entry")
    a.add_argument("--write", metavar="FILE", default=None,
                   help="Merge the entry into this JSON config (e.g. "
                        "~/.claude.json), keeping a backup. Without it, print.")
    a.set_defaults(func=contact_config)

    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    db_mod.init(args.db)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
