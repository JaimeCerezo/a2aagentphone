"""The phone's own records: who may call, who I may call, and what happened.

**One database per phone**, with every table inside it:

    /var/lib/a2aagentphone/<agent>/phone.db
        callers    who may ring this phone   (hashes only)
        contacts   who this phone may ring   (usable tokens)
        calls      what happened, both directions

It was two files until 2026-09-22 -- ``callers.db`` and ``contacts.db`` -- and
the reason was a user boundary: a separate dialer account would hold the
outbound tokens so the answering agent could not read them. **That account does
not exist.** Both files ended up owned by the same user, so the split bought
nothing and cost plenty: two places to look, two connections to open, the same
``calls`` table duplicated in both, and a real person asking why his phone had
two address books.

Jaime called it, and he was right: one phone, one file, the tables inside. If
the dialer daemon is ever built, it gets its own file *then*, for a reason that
will be true rather than aspirational.

A phone keeps everything of its own together, so it works the same whether the
agent runs on the host or inside a container — a machine-wide file simply does
not exist for a container, and mounting one in would mean punching a hole in
the isolation precisely to share everyone else's tokens.

## Why the call log is here rather than in a file

Because of what it is actually for. Auditing is the smaller half; the bigger
half is **making a retry safe**.

Measured on 2026-09-22: a call was killed by its budget cap mid-task, having
committed but not pushed, with the deploy already out. It was re-sent. What
stopped the work being done twice — on a change that was not idempotent — was
the far end keeping its own record and *reading it before touching anything*.

So the question this table has to answer, cheaply, is:

    "has this exact request already run here, and how did it end?"

## The one decision that makes it work: two writes

A row is inserted **when the call arrives, before the agent is started**, and
updated when it ends. That is deliberate. If rows were only written on
completion, a call that died halfway would leave no trace at all — and that is
exactly the call you need to know about.

So ``finished_at IS NULL`` on an old row is not a missing record. It is the
record: *this ran, and nobody ever learned how it ended. Go and look at what it
left behind.*

## Honest limits

- **The answering user can edit its own log.** Nothing here prevents that, and
  nothing can while the listener runs as that user. The log is worth what the
  user is worth, exactly like the token. The fix is the separate-user daemon
  described in DESIGN.md, not a cleverer table.
- **Never copy one of these with ``cp`` while it is in use.** Use SQLite's own
  backup. A torn copy looks fine until the day you need it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

# contacts.db -- who I may call. Tokens usable as-is: this file is the reason
# the dialer wants its own user.
CONTACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  alias      TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  token      TEXT,                  -- NULL = a contact with no credential yet
  expires_at TEXT,
  note       TEXT,
  created_at TEXT NOT NULL
);
"""

# callers.db -- who may call me. Hashes only: a stolen copy of this file does
# not let anyone call anybody.
CALLERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS callers (
  alias        TEXT PRIMARY KEY,
  token_hash   TEXT NOT NULL UNIQUE,
  auth         TEXT NOT NULL,       -- 'token' | 'token+mtls'. Per caller, not
                                    -- per phone: some callers earn a stronger
                                    -- claim than others on the same number.
  scope        TEXT NOT NULL,
  expires_at   TEXT,
  note         TEXT,
  created_at   TEXT NOT NULL,
  revoked_at   TEXT                 -- a date, rather than deleting the row:
                                    -- "who had access in March?" is the
                                    -- question you ask after a scare
);
"""
# There was an `allowed_from TEXT NOT NULL` here until v0.5.0, holding CIDRs the
# credential could be used from. It is dropped on the first open of an existing
# database -- see _migrate. The short version of why: between a container's SNAT
# and a reverse proxy that owns X-Forwarded-For, the value the ear could read was
# never reliably the caller's address, so the column named one thing and held
# another. A field that reads like a control and is not one is worse than its
# absence, because an audit believes it. callers.py has the long version.

# The log. The same shape on both sides -- the ear records 'in', the dialer
# records 'out' -- so that one task_id joins the two machines' accounts of the
# same call. That is how you see a call that completed at the far end while the
# caller was already dead: a finished 'in' row there, an unfinished 'out' row
# here.
CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id              INTEGER PRIMARY KEY,
  direction       TEXT NOT NULL,     -- 'in' = they called me, 'out' = I called
  task_id         TEXT,              -- A2A task id; NULL for a refused call,
                                     -- which never became a task
  context_id      TEXT,              -- A2A thread; several calls share one
  peer            TEXT,              -- who answered, or who called
  caller          TEXT,              -- callers.alias once that table is in use.
                                     -- NULL is the single-token era, and says so
  remote_addr     TEXT,              -- the caller, as best we can know it:
                                     -- the forwarded address when there is a
                                     -- proxy in front, the socket peer when
                                     -- there is not
  via             TEXT,              -- the proxy's own address, when the two
                                     -- differ. Kept because remote_addr then
                                     -- rests on the proxy telling the truth,
                                     -- and the peer is the part we saw
                                     -- ourselves
  auth            TEXT,              -- 'token' | 'token+mtls' | 'none'

  started_at      TEXT NOT NULL,     -- UTC, written BEFORE the agent runs
  finished_at     TEXT,              -- NULL on an old row = we never found out
  outcome         TEXT,              -- NULL = unknown; 'ok', 'auth_failed',
                                     -- 'error_max_budget_usd', 'timeout'...

  claude_session  TEXT,              -- lets you reopen the real transcript
  turns           INTEGER,
  cost_usd        REAL,
  duration_ms     INTEGER,
  denials         INTEGER,

  request_sha256  TEXT,              -- same request, same hash: this is what
                                     -- makes a retry answerable
  request_excerpt TEXT,              -- first 200 chars, to recognise it by eye.
                                     -- Not the whole text: a request can carry
                                     -- anything, and the log is not the place
  cred_prefix     TEXT,              -- refused calls only: first 8 hex of the
                                     -- hash of what was presented. Enough to
                                     -- tell one wrong token trying 500 times
                                     -- from 500 different ones. Never the token
  note            TEXT               -- free text: why it died, what it left
);

CREATE UNIQUE INDEX IF NOT EXISTS calls_task
    ON calls(direction, task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS calls_by_req  ON calls(request_sha256, started_at);
CREATE INDEX IF NOT EXISTS calls_by_time ON calls(started_at);
CREATE INDEX IF NOT EXISTS calls_by_peer ON calls(caller, started_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _group_shared(directory: str | Path) -> bool:
    """Has whoever installed this phone deliberately opened it to a group?

    A phone is normally one machine, one user, and 0700/0600 is exactly right.
    But a machine can run **several system identities that are the same
    agent** -- the interactive session, the identity that fires scheduled work,
    the identity that answers the phone -- and all of them need the contacts and
    the outbound tokens. From outside it is still one number; inside it is three
    processes behind it.

    Found on 2026-09-27, on two machines at once, and it had been failing in
    silence: the identity that *answers* re-tightens the files on **every**
    open, because ``_restrict`` runs from ``_connect`` and not just from
    ``init``. So an operator who opened the directory to a shared group saw it
    work, and then saw the calling identity lose access a few seconds after the
    next answered call -- with no error anywhere, and nothing to notice until
    somebody tried to dial. Reopening the permissions from outside cannot win a
    race against a chmod that runs on every connection.

    The switch is **setgid plus group-write** on the directory, and the setgid
    bit is the load-bearing half. Group-write alone looked like the obvious
    signal and is the wrong one: ``umask 002`` is normal on a machine with
    several identities sharing a tree, so a directory created there is
    group-writable *by accident*, and a fresh install would have opened itself
    up with nobody asking. Caught by the test on 2026-09-27, before release.

    setgid cannot arrive by umask. Somebody has to type ``chmod 2770``, which is
    the deliberate act this is looking for -- and it is the mode that case needs
    anyway, so that a file created by any of the identities keeps the group.

    Nothing changes for a phone nobody has opened up: fresh installs still get
    0700 and 0600.
    """
    try:
        mode = Path(directory).stat().st_mode
    except OSError:
        return False
    return bool(mode & stat.S_ISGID) and bool(mode & stat.S_IWGRP)


def _dir_mode(directory: str | Path) -> int:
    # setgid on the shared variant, so a file created by any of the identities
    # keeps the group instead of falling back to the creator's own.
    return 0o2770 if _group_shared(directory) else 0o700


def _chmod_if_needed(path: str | Path, mode: int) -> None:
    """chmod only when the mode is actually wrong.

    The "only when needed" matters more than it looks: in the shared case the
    mode is already what we want, so no chmod is attempted at all, and the
    non-owner identities never fight over it. And a chmod that genuinely cannot
    be applied says so on stdout rather than vanishing -- the journal is where
    somebody will look, and this project has already paid once for a warning
    nobody could see.
    """
    path = Path(path)
    try:
        if stat.S_IMODE(path.stat().st_mode) == mode:
            return
        path.chmod(mode)
    except OSError as e:
        print(f"a2aagentphone: cannot set mode {mode:o} on {path}: {e}", file=sys.stderr)


def _restrict(path: Path) -> None:
    """Lock down the database and its WAL sidecars.

    ``init`` chmods the database itself, but SQLite creates ``-wal`` and
    ``-shm`` on first write, with whatever the umask says -- and the write-ahead
    log holds the same rows as the database, including a contact's usable
    token. Found on 2026-09-22 with contacts.db-wal sitting at 0644 inside a
    0700 directory: no exposure that time, because the directory saved it, but
    the file mode was a lie about how protected the contents were.

    0600, unless the directory is group-shared on purpose -- see
    ``_group_shared``. The directory is what decides; these files only follow.
    """
    mode = 0o660 if _group_shared(path.parent) else 0o600
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            _chmod_if_needed(candidate, mode)


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL so a reader (someone asking "did this already run?") never blocks the
    # writer recording a call that is arriving right now.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _restrict(Path(path))
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current shape.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so a schema
    change would silently never reach any machine that already had a database --
    the same shape of bug as an update that replaces only the code. Cheap to do
    on every open, and it means a schema change never needs a migration step
    anyone has to remember.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    for column, decl in (("via", "TEXT"),):
        if column not in have:
            conn.execute(f"ALTER TABLE calls ADD COLUMN {column} {decl}")

    # v0.5.0: the origin filter is gone, so the column that fed it goes too.
    #
    # Dropped rather than left in place and ignored. A NOT NULL column called
    # `allowed_from`, still holding the CIDRs somebody typed, sitting in a table
    # nothing consults, is precisely the artefact that makes an operator believe
    # the door still checks where a call came from. The row should not be able
    # to say something the code does not do.
    #
    # DROP COLUMN needs SQLite >= 3.35 (2021). If this is an older library the
    # column simply stays -- unused and harmless -- rather than the phone
    # refusing to open its own database over a tidy-up.
    callers_cols = {r[1] for r in conn.execute("PRAGMA table_info(callers)")}
    if "allowed_from" in callers_cols:
        try:
            conn.execute("ALTER TABLE callers DROP COLUMN allowed_from")
        except sqlite3.Error as e:
            print(
                "a2aagentphone: could not drop the obsolete callers.allowed_from "
                f"column ({e}). It is no longer read; nothing is filtered by it.",
                file=sys.stderr,
            )


def _absorb(conn: sqlite3.Connection, old: Path, tables: tuple[str, ...]) -> bool:
    """Pull an old single-purpose database into the unified one.

    Runs once per file and leaves the original renamed rather than deleted --
    a migration that destroys the only copy is a migration you cannot check
    afterwards.
    """
    if not old.exists():
        return False
    try:
        # Commit first and commit again before detaching. SQLite refuses to
        # ATTACH or DETACH inside an open transaction, and Python's sqlite3
        # opens one implicitly on the first write -- so the first absorb
        # inserted its rows, failed on DETACH, and left the second one unable
        # to attach at all. It looked like "the contacts did not migrate";
        # what actually happened is that the first migration never finished.
        conn.commit()
        conn.execute("ATTACH DATABASE ? AS old", (str(old),))
        for table in tables:
            cols = [r[1] for r in conn.execute(f"PRAGMA old.table_info({table})")]
            if not cols:
                continue
            # Only the columns that still exist here. The old file was written
            # by an older version and can hold fields this one has since
            # dropped -- `allowed_from` is the first, in v0.5.0. Copying the
            # old column list verbatim made the INSERT fail, and the failure is
            # caught below and turned into "did not migrate": the phone would
            # come up with an empty callers table and refuse every call, with
            # nothing in the journal pointing at the real cause.
            here = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in cols if c in here]
            if not cols:
                continue
            names = ", ".join(cols)
            conn.execute(
                f"INSERT OR IGNORE INTO {table} ({names}) SELECT {names} FROM old.{table}"
            )
        conn.commit()
        conn.execute("DETACH DATABASE old")
    except sqlite3.Error:
        try:
            conn.commit()
            conn.execute("DETACH DATABASE old")
        except sqlite3.Error:
            pass
        return False
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(old) + suffix)
        if candidate.exists():
            candidate.rename(str(candidate) + ".migrated")
    return True


def path_for(directory: str | Path) -> Path:
    return Path(directory) / "phone.db"


def init(directory: str | Path) -> Path:
    """Create this phone's database. Safe to run again."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    _chmod_if_needed(d, _dir_mode(d))
    phone = path_for(d)
    with _connect(phone) as c:
        c.executescript(CALLERS_SCHEMA + CONTACTS_SCHEMA + CALLS_SCHEMA)
        _migrate(c)
        # The two-file era. Absorbed on the first open after upgrading, so
        # nobody has to run anything or remember that it happened.
        _absorb(c, d / "callers.db", ("callers", "calls"))
        _absorb(c, d / "contacts.db", ("contacts", "calls"))
    _restrict(phone)
    return phone


def request_digest(text: str) -> tuple[str, str]:
    """Hash and excerpt of a request: what identifies it, and what recognises it."""
    return (
        hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        text[:200],
    )


def begin(db: str | Path, **fields) -> int | None:
    """Record a call that is starting. Returns the row id, or None if the log
    is unavailable.

    **Logging never breaks a call.** A phone that refuses to answer because it
    could not write its own log would be trading the service for the record of
    the service, which is the wrong way round.
    """
    fields.setdefault("started_at", _now())
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    try:
        with _connect(db) as c:
            cur = c.execute(f"INSERT INTO calls ({cols}) VALUES ({marks})", tuple(fields.values()))
            return cur.lastrowid
    except sqlite3.Error:
        return None


def finish(db: str | Path, row_id: int | None, **fields) -> None:
    """Close the row opened by :func:`begin`. Same no-failure rule."""
    if row_id is None:
        return
    fields.setdefault("finished_at", _now())
    sets = ", ".join(f"{k}=?" for k in fields)
    try:
        with _connect(db) as c:
            c.execute(f"UPDATE calls SET {sets} WHERE id=?", (*fields.values(), row_id))
    except sqlite3.Error:
        pass


def previous(db: str | Path, digest: str, direction: str = "in") -> sqlite3.Row | None:
    """The last time this exact request was seen, if ever.

    The payoff of the whole table. A row whose ``finished_at`` is NULL means
    the request ran and nobody learned how it ended -- so before repeating it,
    go and look at what it left behind.
    """
    try:
        with _connect(db) as c:
            return c.execute(
                "SELECT * FROM calls WHERE direction=? AND request_sha256=?"
                " ORDER BY started_at DESC LIMIT 1",
                (direction, digest),
            ).fetchone()
    except sqlite3.Error:
        return None


def unfinished(db: str | Path, limit: int = 20) -> list[sqlite3.Row]:
    """Calls that started and were never closed -- the ones worth looking at."""
    try:
        with _connect(db) as c:
            return c.execute(
                "SELECT * FROM calls WHERE finished_at IS NULL"
                " ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []
