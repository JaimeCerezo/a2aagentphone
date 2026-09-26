"""The mouth: an MCP server that lets an agent call another one.

``server.py`` is the ear -- it listens and wakes this machine's agent. This is
the other half: it gives an agent the ability to **call someone else** without
leaving its conversation.

A minimal MCP server exposing one tool. The agent says who to call and what to
ask; this builds the A2A request, attaches the credential and hands back the
answer.

**Why an MCP and not a documented curl**: because this way the agent cannot
call anyone who is not in its contacts. The list of allowed destinations stops
being a rule written in a prompt and becomes the only way to dial.

## What this version is not, yet

A **fixed-destination prototype**: the contact list is two environment
variables and there is exactly one friend. The finished shape splits this in
two, with a user boundary in the middle:

    agent  ->  [this MCP, no secrets]  ->  socket  ->  [daemon holding the keys]

Because today the token sits in the environment of a process the agent itself
started, which means **the agent could read it** -- and so could anything that
talks the agent into printing its environment. It measures what it measures:
enough to prove the call works, not enough to put in front of anything real.
See DESIGN.md.

## Wiring it to an agent

    claude --mcp-config config.json --allowedTools "mcp__a2aagentphone__ask_agent"

where ``config.json`` declares this module as a ``stdio`` server.
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

import httpx
from mcp.server.mcpserver import MCPServer

import sqlite3
from datetime import datetime, timezone

# **One way to have contacts**: the phone's own database.
#
#   A2A_DB=/var/lib/a2aagentphone/<phone>
#
# There used to be a second shape -- A2A_FRIEND / A2A_URL / A2A_TOKEN, one
# fixed destination in three environment variables -- and keeping both around
# was a mistake that took a real person asking "why does my phone have two
# address books?" to see. The two halves of a machine had drifted onto
# different mechanisms: the incoming side moved to the database when the unit
# file gained --db, while the outgoing side sat in the user's own MCP config,
# which no update has ever touched.
#
# The environment shape is gone. What is left is the table: a row that can be
# added, replaced, listed and revoked, and an agent that still cannot dial
# anyone who is not in it -- because this tool takes a NAME, never an address.
DB = os.environ.get("A2A_DB")
TIMEOUT = float(os.environ.get("A2A_TIMEOUT", "300"))
LEGACY = {k for k in ("A2A_FRIEND", "A2A_URL", "A2A_TOKEN", "A2A_TOKEN_FILE")
          if os.environ.get(k)}


def _contacts() -> dict[str, dict]:
    """Everyone this phone may call, by name."""
    if not DB:
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB}/phone.db?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return {r["alias"]: dict(r) for r in conn.execute("SELECT * FROM contacts")}
    except sqlite3.Error:
        return {}


def _log(direction: str, **fields) -> int | None:
    """Record an outgoing call in this phone's own log.

    The half that was missing, and the reason it matters: on 2026-09-22 a call
    completed perfectly at the far end while the caller died collecting the
    answer. The work was done; the acknowledgement was lost. A row written
    before dialling turns that into something answerable -- an unfinished
    'out' row here, against a finished 'in' row there, joined by task_id.
    """
    if not DB:
        return None
    try:
        from . import db as db_mod

        return db_mod.begin(f"{DB}/phone.db", direction=direction, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks a call
        return None


# The names known AT STARTUP, and only for the instructions below -- what the
# model is told exists. Everything that actually dials re-reads the table, so
# the credential in hand is never a stale copy.
#
# The difference is not academic. Until v0.3.4 the whole contact list was read
# once, here, at import time: a token rotated afterwards did not reach a
# running dialer, which then presented the old one and got a 401 -- an error
# that reads as "the far end rejected me" when the truth is "I called with a
# credential I had cached". It happened between Aris and scm-intranet on
# 2026-09-22 and cost both ends a debugging session each.
#
# It also contradicted the reason SQLite was chosen over files, which DESIGN.md
# states plainly: "it queries on each call and sees the current state -- add a
# friend and the next call already finds it, with nothing to reload". True of
# the ear; it was not true of the mouth.
STARTUP_NAMES = sorted(_contacts())

server = MCPServer(
    name="a2aagentphone",
    instructions=(
        "Lets you phone another agent. You can only call the names in this "
        "list, and there is no way to pass an address instead: "
        + (", ".join(STARTUP_NAMES) if STARTUP_NAMES
           else "nobody is in the contact list yet.")
        + ". The list is read fresh on every call, so a name added after this "
        "session started is callable even if it is missing here -- "
        "list_contacts is the current answer."
    ),
)


# Said once, loudly, rather than by quietly falling back to the old behaviour.
# A machine still configured the old way must be told so -- silently working
# through a second mechanism is how the two halves came apart in the first
# place.
MISCONFIGURED = None
if not DB:
    MISCONFIGURED = (
        "ERROR: this phone is configured the old way"
        + (f" ({', '.join(sorted(LEGACY))} in the environment)" if LEGACY else "")
        + ". Contacts now live in the phone's database. Fix it once:\n"
        "  sudo a2aagentphone-admin --db /var/lib/a2aagentphone/<phone> contact add <name> \\\n"
        "       --url https://<their phone>/ --token-file <path to their token>\n"
        "  sudo a2aagentphone-admin --db /var/lib/a2aagentphone/<phone> contact config <name> "
        "--write ~/.claude.json\n"
        "The second command rewrites this MCP entry to use A2A_DB."
    )


def _destination(who: str) -> tuple[str, str, str] | str:
    """Resolve a name to (name, url, token), or return an error to show.

    Reads the table **now**, not at import. A dialer that caches credentials
    hands out yesterday's token and blames the far end for refusing it.
    """
    if MISCONFIGURED:
        return MISCONFIGURED
    contacts = _contacts()
    if who in contacts:
        row = contacts[who]
        if not row.get("token"):
            return f"ERROR: «{who}» is in the contact list with no credential yet."
        return who, row["url"], row["token"]
    known = ", ".join(sorted(contacts)) or "nobody"
    return f"ERROR: «{who}» is not in the contact list. You may call: {known}."


@server.tool(
    name="ask_agent",
    title="Ask another agent",
    description=(
        "Call another agent by name and return its answer verbatim. The agent "
        "on the other end is a Claude session with its own project, its own "
        "memory and its own files: ask it what only it can know, not general "
        "knowledge. A call takes seconds, costs money and starts an agent on "
        "someone else's machine, so one well-formed question is worth more "
        "than three probes."
    ),
)
async def ask_agent(who: str, question: str) -> str:
    """Call ``who`` and return whatever they answer."""
    resolved = _destination(who)
    if isinstance(resolved, str):
        return resolved
    name, url, token = resolved

    from . import db as db_mod

    digest, excerpt = db_mod.request_digest(question)
    task = str(uuid.uuid4())
    # Written before dialling, not after: a caller that dies collecting the
    # answer would otherwise leave no trace of having called at all.
    row = _log("out", task_id=task, peer=name, request_sha256=digest,
               request_excerpt=excerpt, auth="token")

    def close(**fields):
        if row is not None and DB:
            db_mod.finish(f"{DB}/phone.db", row, **fields)

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": question}],
                "messageId": task,
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        # Without this the server assumes protocol version 0.3 and rejects the
        # call with an error that does not say what it is complaining about.
        "A2A-Version": "1.0",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        # Named separately because it is the most likely failure and the least
        # obvious: httpx timeout exceptions stringify to the empty string.
        close(outcome="timeout",
              note="may still be running at the far end; check before retrying")
        return (
            f"ERROR: {name} did not answer within {TIMEOUT:g}s. The agent may "
            "still be working -- and may still finish the job. Do NOT simply "
            "re-send anything that changes things; ask what it did."
        )
    except httpx.HTTPError as e:
        detail = str(e) or type(e).__name__
        close(outcome="unreachable", note=detail[:500])
        return f"ERROR: could not reach {name}: {detail}"

    if response.status_code == 401:
        close(outcome="rejected")
        return (
            f"ERROR: {name} rejected the credential (401). It may have expired, "
            "been revoked, or this machine may not be on its allowed list."
        )
    if response.status_code != 200:
        close(outcome=f"http_{response.status_code}")
        return f"ERROR: {name} answered HTTP {response.status_code}."

    data = response.json()
    if "error" in data:
        close(outcome="protocol_error", note=str(data["error"])[:500])
        return f"ERROR from the protocol: {data['error'].get('message')}"

    status = data.get("result", {}).get("task", {}).get("status", {})
    parts = (status.get("message") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()

    # The cost is handed back on purpose: whoever places a call should know
    # what it spends -- including calls that failed, because those spent too.
    meta = (status.get("message") or {}).get("metadata") or {}
    cost = meta.get("cost_usd")
    close(outcome="ok" if status.get("state") == "TASK_STATE_COMPLETED" else "failed",
          cost_usd=cost, turns=meta.get("num_turns"),
          duration_ms=meta.get("duration_ms"))
    footer = f"\n\n[{name} · {cost:.4f} USD]" if cost else f"\n\n[{name}]"

    if status.get("state") != "TASK_STATE_COMPLETED":
        return f"ERROR: {name} could not answer. {text}" + footer

    return (text or "(empty answer)") + footer


@server.tool(
    name="list_contacts",
    title="Who you can call",
    description="List the agents this phone may call, with what is known about "
                "each. Cheap and local: it makes no call.",
)
async def list_contacts() -> str:
    if MISCONFIGURED:
        return MISCONFIGURED
    contacts = _contacts()
    if not contacts:
        return "The contact list is empty. This phone cannot call anyone yet."
    lines = []
    for name in sorted(contacts):
        row = contacts[name]
        cred = "credential held" if row.get("token") else "NO CREDENTIAL"
        lines.append(f"{name}  {row['url']}  [{cred}]"
                     + (f"  {row['note']}" if row.get("note") else ""))
    return "\n".join(lines)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
