"""``a2aagentphone-log`` — read a phone's call log.

A log nobody can read is a log nobody reads. Three questions, no SQL:

    a2aagentphone-log                     the last calls, newest first
    a2aagentphone-log --open              started and never finished
    a2aagentphone-log --same "<text>"     has this exact request run here before?

The last one is the point of the table. Before re-sending something that is not
idempotent, ask: an unfinished row means it ran and nobody learned how it
ended, so go and look at what it left behind.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path("/var/lib/a2aagentphone")


def _phone_dir(name: str | None) -> Path:
    if name:
        return ROOT / name
    # Directories only. /var/lib/a2aagentphone also holds mailbox.md, and taking
    # that for a phone produced "no log at .../mailbox.md/callers.db".
    dirs = sorted(p for p in ROOT.glob("*") if p.is_dir())
    if not dirs:
        sys.exit(f"a2aagentphone-log: no phone found under {ROOT}")
    return dirs[0]


def _show(r: sqlite3.Row) -> None:
    state = r["outcome"] or "UNFINISHED"
    cost = f"{r['cost_usd']:.4f}" if r["cost_usd"] is not None else "-"
    # Who the other end was, and it lives in a different column each way:
    # an inbound row knows the caller, an outbound row knows the peer it rang.
    who = (r["peer"] if r["direction"] == "out"
           else r["caller"] or r["remote_addr"]) or "?"
    via = f" via {r['via']}" if r["via"] else ""
    print(f"{r['started_at']}  {r['direction']:3} {state:24} {cost:>8}  {who}{via}")
    if r["request_excerpt"]:
        print(f"    {r['request_excerpt'][:100]}")
    if r["note"]:
        print(f"    note: {r['note']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="a2aagentphone-log", description=__doc__)
    ap.add_argument("--phone", default=None)
    ap.add_argument("--open", action="store_true", dest="open_",
                    help="calls that started and never finished")
    ap.add_argument("--same", metavar="TEXT", default=None,
                    help="has this exact request run here before?")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    db = _phone_dir(args.phone) / "phone.db"
    if not db.exists():
        sys.exit(f"a2aagentphone-log: no log at {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    if args.open_:
        rows = conn.execute(
            "SELECT * FROM calls WHERE finished_at IS NULL ORDER BY started_at DESC"
        ).fetchall()
        if not rows:
            print("no unfinished calls.")
            return 0
        print(f"{len(rows)} call(s) started and never finished.")
        print("These ran; nobody learned how they ended. Check what they left.\n")
    elif args.same is not None:
        digest = hashlib.sha256(args.same.encode("utf-8", "replace")).hexdigest()
        row = conn.execute(
            "SELECT * FROM calls WHERE request_sha256=? ORDER BY started_at DESC LIMIT 1",
            (digest,),
        ).fetchone()
        if row is None:
            print("not seen before -- safe to send.")
            return 0
        _show(row)
        if row["finished_at"] is None:
            print("\nWARNING: this exact request ran and never finished.")
            print("Do NOT simply re-send it if it changes anything.")
        return 0
    else:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (args.limit,)
        ).fetchall()
        if not rows:
            print("no calls logged yet.")

    for r in rows:
        _show(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
