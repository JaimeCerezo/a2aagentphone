"""The ear: an A2A endpoint that wakes this machine's agent.

One server per agent. It listens on a port and, when a call arrives, runs the
``claude`` installed on this machine and returns whatever it answers.

Two deliberate properties, and they are the reason this exists rather than a
wrapper around something else:

  1. **It calls the machine's own ``claude``**, not one bundled inside an npm
     dependency. The agent that answers the phone has to be the same one that
     works in tmux, with its version, its memory and its configuration.
  2. **The card advertises the public address it is told**, not the interface
     it binds to. Without that, behind a proxy it publishes a number nobody
     can dial.

Everything about the protocol itself -- card, task states, artifacts, JSON-RPC
-- comes from the official A2A SDK. What lives here is the part nobody gives
away: how you reach the local agent.

Scope of this version: answer a call by starting a fresh agent, and keep the
thread if the caller comes back. It does not talk to an already-running tmux
session, it does not sign its card, and it has no callback webhook. See
DESIGN.md for what is decided and still unbuilt.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import hmac
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    SecurityRequirement,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from starlette.applications import Starlette
from starlette.middleware import Middleware

from . import __version__, callers as callers_mod, db

log = logging.getLogger("a2aagentphone.server")

# The caller's address, carried from the ASGI layer to the executor. It is
# known where the connection is accepted and needed where the call is logged,
# and there is nothing in between that passes it: the A2A RequestContext
# describes the message, not the socket. A context variable is the narrow way
# across -- it follows the request's own task and never leaks into another's.
#
# Today this is the ONLY attribution there is. With a single shared token the
# log cannot say *who* called, only from where. The callers table is what turns
# that into a name.
_remote = contextvars.ContextVar("a2aagentphone_remote", default=None)
# The caller's registered name, once there is one to know.
_who = contextvars.ContextVar("a2aagentphone_caller", default=None)


def _caller_address(scope) -> tuple[str | None, str | None]:
    """Who called, and who told us so.

    Behind a reverse proxy the socket peer is the proxy, which is nobody: the
    first real call scm-intranet logged recorded 172.18.0.2, the Docker bridge,
    for a caller out on the public internet. Useless for attribution, and
    attribution is most of what the address is for.

    So the forwarded address wins when there is one -- and the peer is kept
    alongside it, because that swap is a change of evidence. The peer is what
    this process saw with its own eyes; the forwarded value is what something
    upstream said, and a caller that already holds a token could put anything
    in that header. Keeping both means a log entry can be read for what it is
    rather than trusted flat.
    """
    peer = (scope.get("client") or (None, None))[0]
    forwarded = None
    for key, value in scope.get("headers") or []:
        if key == b"x-forwarded-for":
            # Leftmost is the original client; the rest are the proxies it
            # crossed on the way.
            forwarded = value.decode("latin-1").split(",")[0].strip() or None
            break
    if forwarded and forwarded != peer:
        return forwarded, peer
    return peer, None

BEARER = "bearer"

# What the agent inherits from the server's environment. Kept deliberately
# short: the rest of the server's environment is none of the agent's business.
# HOME is in, because that is where its memory and its credentials live.
#
# CLAUDE_CODE_OAUTH_TOKEN is in for the same reason HOME is -- it is the other
# place a credential can live -- and it was added in v0.5.1 because leaving it
# out forced every phone onto the fragile one. Concretely, and measured:
#
#   * A stored login (`~/.claude/.credentials.json`) carries a REFRESH token,
#     and a refresh token ROTATES when it is used. Two system identities of the
#     same agent handed a copy of the same file -- which is exactly what
#     splitting one account into `-agent` / `-cron` / `-phone` produces -- will
#     invalidate each other the first time either refreshes.
#   * A setup token does not refresh, so it does not rotate, so it cannot
#     collide. But it arrives as an environment variable, and this list is what
#     decided whether the answering agent ever saw it. It did not.
#
# On 2026-09-29 that combination had a phone unable to answer for 30 hours with
# nobody aware, because the failure is invisible from the answering side: the
# call arrives, AUTHENTICATES CORRECTLY, and dies afterwards when the agent
# starts. See the credential check in `main`.
#
# This exposes nothing new to the agent. It could already read the credentials
# in its own $HOME; this is the same secret reachable by another name.
_INHERITED = (
    "PATH", "HOME", "LANG", "TERM", "SHELL", "USER", "LOGNAME", "TZ",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _credential_state() -> tuple[bool, str]:
    """Can the agent this phone launches authenticate at all?

    Answered at startup, out loud, because the answer is otherwise unknowable
    until somebody calls -- and a phone that cannot answer does not complain.
    It is the caller who finds out, and what the caller sees ("Failed to
    authenticate: OAuth session expired") points at the phone's own token,
    which is the one thing that was fine.

    Deliberately not fatal. A phone whose credential is being repaired should
    keep its port open and keep logging refused calls; refusing to start would
    only remove the evidence.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return True, "CLAUDE_CODE_OAUTH_TOKEN"
    stored = Path(os.environ.get("HOME", "~")).expanduser() / ".claude/.credentials.json"
    try:
        body = json.loads(stored.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, f"no {stored}, and CLAUDE_CODE_OAUTH_TOKEN is not set"
    oauth = body.get("claudeAiOauth") or body
    # A failed refresh rewrites this file WITHOUT the tokens, keeping the other
    # keys. So "the file exists" is not the question; "does it still hold
    # anything usable" is. That is the shape the 2026-09-29 outage left behind.
    if oauth.get("accessToken") or oauth.get("refreshToken"):
        return True, str(stored)
    return False, f"{stored} exists but holds no token -- a refresh failed and emptied it"


# --------------------------------------------------------------------------
# The card: the phone number you publish
# --------------------------------------------------------------------------
def build_card(url: str, *, name: str, description: str, auth: bool) -> AgentCard:
    """Build the agent card served at ``/.well-known/agent-card.json``."""
    card = AgentCard(
        name=name,
        description=description,
        version=__version__,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        skills=[
            AgentSkill(
                id="ask",
                name="Ask the agent",
                description="Ask this agent about its own project: what it is "
                "working on, what is pending, what it knows about its machine.",
                tags=["agent", "query"],
                examples=["What is pending?", "Which project do you look after?"],
            )
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    if auth:
        card.security_schemes[BEARER].http_auth_security_scheme.scheme = "bearer"
        requirement = SecurityRequirement()
        requirement.schemes[BEARER].SetInParent()
        card.security_requirements.append(requirement)
    return card


# --------------------------------------------------------------------------
# Authentication: the callers table, and nothing else
# --------------------------------------------------------------------------
class BearerAuth:
    """Require ``Authorization: Bearer <token>`` on everything but the card.

    The card is served unauthenticated on purpose: a caller reads it to *find
    out* which credential it needs, before holding any.

    **There is exactly one place a credential can live: the ``callers`` table.**

    Until v0.3.0 there was a second -- a single shared token in a file, kept as
    a fallback so that a phone installed before the table existed would carry
    on answering. It outlived its purpose and was three things wearing one hat,
    each of which turned out to be a liability:

      * **It was the switch that mounted this middleware at all.** A phone
        started without the file did not merely lose the fallback; it answered
        *everybody*, with no authentication whatsoever.
      * **It carried a phone-wide expiry** which refused to start the entire
        service -- enforced long after the credential itself had stopped being
        used by anyone, so a machine would die on a date belonging to a token
        nobody had presented in a month.
      * **And the fallback was conditioned on there being an unrevoked
        caller**, so revoking the last caller did not close the door: it
        silently reopened the shared token, which was still sitting on disk
        with its original value. Revocation has to close, not step backwards.

    Found on 2026-09-22, which is why the file is gone rather than deprecated.

    Now: no row, no entry. An empty table is a phone that refuses every call --
    the correct posture for a phone nobody has been admitted to yet. Expiry is
    per caller, in ``callers.expires_at``, where it belongs, and revoking the
    last caller leaves nothing behind it.
    """

    def __init__(
        self,
        app,
        *,
        log_db: str | None = None,
        callers_db: str | None = None,
    ) -> None:
        self.app = app
        self._callers_db = callers_db
        # Refused calls are the security-relevant rows, and until v0.1.20 they
        # left no trace anywhere at all.
        self._log_db = log_db

    def _refused(self, scope, outcome: str, presented: bytes) -> None:
        if not self._log_db:
            return
        # The first 8 hex of the hash of what was presented -- never the value.
        # Enough to tell one wrong token trying five hundred times from five
        # hundred different ones, which is the difference between somebody's
        # stale config and somebody sweeping.
        prefix = (
            hashlib.sha256(presented).hexdigest()[:8] if presented else None
        )
        addr, via = _caller_address(scope)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.begin(
            self._log_db,
            direction="in",
            outcome=outcome,
            remote_addr=addr,
            via=via,
            auth="none",
            cred_prefix=prefix,
            finished_at=now,
        )

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith("/.well-known/"):
            await self.app(scope, receive, send)
            return

        presented = b""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                presented = value
                break

        parts = presented.split(None, 1)
        bearer = parts[1].strip() if len(parts) == 2 and parts[0].lower() == b"bearer" else b""

        # One question, asked once: does this credential belong to a caller who
        # is still admitted, right now? A hash match gives a NAME, which is
        # what makes the log attribution and what makes revoking one caller
        # possible without breaking the others.
        #
        # Note there is no `count()` here any more. It used to gate the
        # fallback, and gating anything on "are there unrevoked callers" is how
        # revoking the last one reopened the shared token.
        #
        # And since v0.5.0 the address is not part of the question. It is still
        # resolved, because the log wants it, but nothing is decided on it: see
        # callers.py for why a pin the network path rewrites was removed
        # instead of documented around.
        address = _caller_address(scope)
        caller = (
            callers_mod.identify(self._callers_db, bearer)
            if bearer and self._callers_db
            else None
        )
        if caller is None:
            # Deliberately the same refusal whatever failed -- nothing
            # presented, unknown, revoked or expired. Saying *which* check
            # failed tells whoever is probing which part they got right.
            self._refused(scope, "auth_failed", bearer)
            await _refuse(send, b'{"error":"unauthorized"}')
            return

        _who.set(caller["alias"])
        # And the address, which this branch used to drop on the floor: the
        # single-token path recorded it and the named path did not, so the log
        # held an address for everyone *except* the callers it could name.
        _remote.set(address)
        log.info("call from caller=%s scope=%s", caller["alias"], caller["scope"])
        await self.app(scope, receive, send)


async def _refuse(send, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------
# The engine: this machine's claude, in the agent's own folder
# --------------------------------------------------------------------------
class LocalClaude:
    """Run the machine's ``claude`` headless and return its JSON result."""

    def __init__(
        self,
        *,
        cwd: str,
        executable: str = "claude",
        timeout: float = 300.0,
        allowed_tools: str | None = None,
        full_permissions: bool = False,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ) -> None:
        self.cwd = str(Path(cwd).resolve())
        self.executable = executable
        self.timeout = timeout
        # ``--allowedTools`` ADDS to what is auto-approved. It does not subtract
        # anything, and this was documented backwards until 2026-09-22, when
        # scm-intranet reproduced the launch and measured it:
        #
        #   * the model's catalogue is NOT trimmed -- with "Read,Glob,Grep" it
        #     still sees Bash, Write, Edit, Agent, WebFetch. **An agent cannot
        #     tell from its own tool list whether it has been restricted.**
        #   * what stops a tool is the permission layer: anything needing
        #     approval dies for want of a TTY. So Write died -- but Bash ran.
        #
        # A phone launched with "Read,Glob,Grep" was therefore never read-only:
        # it could run arbitrary read shell. If you want a tool genuinely
        # barred, ``permissions.deny`` is the thing that binds. And note the
        # asymmetry: in an untrusted workspace ``permissions.allow`` is ignored
        # in silence, while ``deny`` is still honoured.
        self.allowed_tools = allowed_tools
        # The other half of the same discovery, and the reason this flag has to
        # exist. Without it the phone answers with a mutilated agent: it reads,
        # it runs read-only shell, and everything it was opened up to DO --
        # write a file, commit, deploy, sudo -- dies unapproved, because there
        # is nobody at a terminal to approve it.
        #
        # This is the switch that makes the principle real: all the policy is
        # at the door, and inside the room the agent works with everything it
        # has. It is also the most consequential switch on the machine -- any
        # admitted caller gets what the phone's user has, with no prompt in the
        # way. That is the deal, and it is why the controls belong on the token
        # (who, from where, until when, how much) and on which user answers.
        # If you want a phone that cannot do this, do not cripple this one:
        # give a less privileged user its own number.
        self.full_permissions = full_permissions
        # The clock stops it hanging; these stop it running away. They are not
        # the same limit: a busy agent spends plenty well within its deadline.
        #
        # Of the two, the budget is the one that means what you want. A turn
        # cap is a proxy -- one turn can be expensive and a cheap question can
        # need six -- while money is the thing actually being spent.
        #
        # Honest limitation, measured: the cap is checked BETWEEN turns, not
        # before spending. With a 0.01 cap a call still spent 0.064, because
        # the first turn runs to completion whatever it costs. It bounds the
        # runaway, not the single expensive answer.
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd

    def _env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k in _INHERITED}

    def _budget_notice(self) -> str:
        """Tell the agent what it has to spend, so it can land instead of crash.

        The cap is checked BETWEEN turns and the turn in flight always runs to
        completion, so the process is killed wherever it happens to be. While
        the phone only answered questions that was harmless -- a dead call and
        a wasted dollar. Once it can act it is not: measured on 2026-09-22, a
        call died mid-task having committed but not pushed, and having already
        deployed, so the live site and the repository disagreed for minutes
        with nothing raising an error anywhere.

        No flag prevents that, because the kill is external and abrupt. The
        only thing that can stop half-done work is the agent choosing to finish
        cleanly while it still has money -- and for that it has to know. So we
        tell it, in the prompt, and ask for the ordering that survives being
        cut: push before you deploy, because what is pushed is the only thing
        that outlives the session.
        """
        return (
            "\n\n---\n"
            f"[a2aagentphone] You have ${self.max_budget_usd:.2f} for this call. It "
            "is checked between turns and the turn in flight is never "
            "interrupted, so when it runs out you are killed where you stand "
            "-- no cleanup, no final message.\n"
            "If you are changing anything, work so that being cut is survivable: "
            "commit and PUSH before you deploy, and prefer one complete small "
            "step to a large half-done one. If you judge you are running short, "
            "STOP, leave things consistent, and say what is left. A partial "
            "answer that names what is missing is worth far more than being "
            "killed mid-write."
        )

    async def ask(self, prompt: str, *, resume: str | None) -> dict:
        if self.max_budget_usd:
            prompt = prompt + self._budget_notice()
        args = [self.executable, "-p", prompt, "--output-format", "json"]
        if self.allowed_tools:
            args += ["--allowedTools", self.allowed_tools]
        if self.full_permissions:
            args += ["--dangerously-skip-permissions"]
        if self.max_turns:
            args += ["--max-turns", str(self.max_turns)]
        if self.max_budget_usd:
            args += ["--max-budget-usd", str(self.max_budget_usd)]
        if resume:
            # Continuity by explicit identifier. Never --continue: that grabs
            # "the most recent transcript" for the (user, folder) pair, which
            # is exactly how you steal the thread from a live session.
            args += ["--resume", resume]
        else:
            # New session with an id we choose, so we know it from the start
            # and can resume it later without guessing.
            args += ["--session-id", str(uuid.uuid4())]

        log.info("running: %s (cwd=%s)", " ".join(args[:4]), self.cwd)
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            env=self._env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError:
            # Kill it. A timeout that only drops the HTTP connection leaves the
            # agent running and spending money on an answer nobody will read.
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"the agent did not answer within {self.timeout:g}s"
            ) from None

        # Parse stdout BEFORE looking at the exit code. claude exits non-zero
        # for ordinary outcomes -- running out of turns is one -- and writes
        # nothing at all to stderr, but it does leave a complete JSON result on
        # stdout saying exactly what happened and what it cost.
        #
        # Raising on the exit code alone throws away the only explanation there
        # is, and bills a call whose cost nobody ever sees. Measured: a call
        # that hit --max-turns returned exit 1, empty stderr, and a stdout
        # carrying subtype=error_max_turns and total_cost_usd=0.081.
        payload: dict | None = None
        try:
            payload = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            payload = None

        if payload is not None:
            return payload

        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:500]
            raise RuntimeError(
                f"claude exited with code {proc.returncode} and said nothing"
                + (f": {detail}" if detail else "")
            )
        raise RuntimeError("claude returned no readable JSON")


# --------------------------------------------------------------------------
# The translation: from what claude says to what A2A understands
# --------------------------------------------------------------------------
class PhoneExecutor(AgentExecutor):
    def __init__(self, engine: LocalClaude, log_db: str | None = None) -> None:
        self._engine = engine
        self._log_db = log_db
        # A2A contextId -> claude session_id. This is what turns a handful of
        # separate calls into one conversation.
        #
        # It lives in memory: restart the server and threads in flight lose
        # their history. Persisting it is a known gap, not an oversight.
        self._sessions: dict[str, str] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        # The stream MUST open with a Task before any status update.
        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
        await updater.start_work()

        prompt = _text_of(context)
        if not prompt:
            await updater.failed(
                message=updater.new_agent_message([Part(text="empty message")])
            )
            return

        # The row goes in BEFORE the agent runs, and this is the whole design.
        # If rows were only written on completion, a call killed halfway would
        # leave no trace -- and that is precisely the call you need to know
        # about. An old row with finished_at NULL is not a missing record: it
        # is the record. It says this ran and nobody ever learned how it ended,
        # so go and look at what it left behind.
        digest, excerpt = db.request_digest(prompt)
        row = db.begin(
            self._log_db,
            direction="in",
            task_id=task_id,
            context_id=context_id,
            auth="token",
            caller=_who.get(),
            remote_addr=(_remote.get() or (None, None))[0],
            via=(_remote.get() or (None, None))[1],
            request_sha256=digest,
            request_excerpt=excerpt,
        ) if self._log_db else None

        try:
            result = await self._engine.ask(
                prompt, resume=self._sessions.get(context_id)
            )
        except Exception as e:  # noqa: BLE001 - every failure is reported to the caller
            log.exception("the call failed")
            db.finish(self._log_db, row, outcome="crashed", note=str(e)[:500])
            await updater.failed(message=updater.new_agent_message([Part(text=str(e))]))
            return

        session_id = result.get("session_id")
        if session_id:
            self._sessions[context_id] = session_id

        # Metadata the caller is glad to have: what it cost, how many turns, and
        # what the agent was denied -- which is the clue that a restricted scope
        # is cutting something off. Built before the error branch on purpose: a
        # call that failed still spent money, and hiding that is how a bill
        # arrives with no matching record.
        meta = {
            "cost_usd": result.get("total_cost_usd"),
            "num_turns": result.get("num_turns"),
            "claude_session_id": session_id,
            "permission_denials": result.get("permission_denials") or [],
            "duration_ms": result.get("duration_ms"),
        }

        # And the same facts written down on THIS machine, which until now only
        # logged the prompt. Reported by scm-intranet on 2026-09-22 from the
        # side nobody had considered: cost, turns and denials went to the
        # *caller* and nowhere else, so the machine that ran the work could not
        # say how its own call ended. Twice in one day an operator could not
        # tell why a call had died on the machine where it died.
        #
        # This is not the call log -- that needs the callers table, to say WHO
        # rang. It is the half that costs one line and removes the blindness.
        outcome = result.get("subtype") or ("error" if result.get("is_error") else "ok")
        log.info(
            "call finished: context=%s session=%s outcome=%s turns=%s "
            "cost=%s duration_ms=%s denials=%d",
            context_id,
            session_id,
            outcome,
            meta["num_turns"],
            meta["cost_usd"],
            meta["duration_ms"],
            len(meta["permission_denials"]),
        )

        # Closing the row opened before the agent ran. A note on the outcomes
        # that can leave work half done, because a bare 'error_max_budget_usd'
        # does not tell whoever reads this later that the machine may be
        # sitting in an inconsistent state.
        note = None
        if outcome == "error_max_budget_usd":
            note = ("killed between turns by the budget cap -- may have left "
                    "work half done; check before retrying")
        db.finish(
            self._log_db,
            row,
            outcome=outcome,
            claude_session=session_id,
            turns=meta["num_turns"],
            cost_usd=meta["cost_usd"],
            duration_ms=meta["duration_ms"],
            denials=len(meta["permission_denials"]),
            note=note,
        )

        text = (result.get("result") or "").strip() or "(no answer)"

        if result.get("is_error"):
            # Say what actually went wrong. The caller cannot see this machine,
            # so "it failed" costs them a round trip they may not be able to
            # make. error_max_turns in particular is not a malfunction -- it is
            # the turn cap doing its job, and the fix is a narrower question or
            # a higher cap, which only the caller can choose between.
            subtype = result.get("subtype") or "unknown"
            turns = result.get("num_turns")
            if subtype == "error_max_turns":
                reason = (
                    f"the agent ran out of turns (cap reached after {turns}). "
                    "Ask something narrower, or the operator can raise --max-turns."
                )
            elif subtype == "error_max_budget_usd":
                reason = (
                    "the agent hit its spending cap for this call. Ask something "
                    "narrower, or the operator can raise --max-budget-usd. "
                    "WARNING: the cap is checked between turns and the turn in "
                    "flight runs to completion, so if this agent can write, it "
                    "was killed wherever it stood -- it may have left work half "
                    "done. Check the state on that machine before retrying."
                )
            else:
                reason = f"the agent failed: {subtype}"
            if result.get("result"):
                reason += f" — {text}"
            await updater.failed(
                message=updater.new_agent_message([Part(text=reason)], metadata=meta)
            )
            return
        await updater.add_artifact([Part(text=text)], name="response")
        await updater.complete(
            message=updater.new_agent_message([Part(text=text)], metadata=meta)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # No cancellation yet: a headless query is short, and killing it midway
        # leaves the agent unable to report what it already did.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.failed(
            message=updater.new_agent_message(
                [Part(text="this version cannot cancel a call in progress")]
            )
        )


def _text_of(context: RequestContext) -> str:
    message = context.message
    if message is None:
        return ""
    return "\n".join(p.text for p in message.parts if getattr(p, "text", "")).strip()


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A2A phone for a single agent.")
    ap.add_argument("--cwd", required=True, help="The agent's project folder.")
    ap.add_argument("--name", default=None, help="Agent name shown on the card.")
    ap.add_argument("--description", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9110)
    ap.add_argument(
        "--public-url",
        default=None,
        help="Address advertised on the card. Defaults to where it listens.",
    )
    ap.add_argument("--claude", default="claude", help="Path to the claude to use.")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument(
        "--allowed-tools",
        default=None,
        help="Comma-separated tool list passed straight to claude "
        "--allowedTools. NOTE, measured: this ADDS to what is auto-approved, "
        "it does not restrict. The agent still sees every tool, and read-only "
        "shell still runs. To bar a tool, use permissions.deny.",
    )
    ap.add_argument(
        "--full-permissions",
        action="store_true",
        help="Answer with --dangerously-skip-permissions, so the agent can "
        "actually act: write, commit, deploy, sudo. Without it the phone is a "
        "mutilated agent, because nothing that needs approval can be approved "
        "-- there is no terminal. This gives any admitted caller what the "
        "phone's user has: put the controls on the token, and give a less "
        "privileged user its own number if you want a restricted phone.",
    )
    ap.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Cap on agent turns per call. A proxy for cost: prefer "
        "--max-budget.",
    )
    ap.add_argument(
        "--max-budget",
        type=float,
        default=None,
        metavar="USD",
        help="Cap on what one call may spend, in dollars. Better than a turn "
        "cap, which is only a proxy. Checked between turns, so a single "
        "expensive turn can overshoot it.",
    )
    ap.add_argument(
        "--db",
        required=True,
        metavar="DIR",
        help="Directory holding this phone's database. Created if missing. "
        "REQUIRED: this is where the callers live, so without it there is no "
        "credential that could ever be accepted -- and no record of who "
        "called. It was optional until v0.3.0, when the single token file it "
        "used to fall back to was removed.",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        print(f"error: no such folder: {cwd}", file=sys.stderr)
        return 2

    url = args.public_url or f"http://{args.host}:{args.port}/"
    if not url.endswith("/"):
        url += "/"

    name = args.name or cwd.name
    card = build_card(
        url,
        name=name,
        description=args.description
        or f"Agent for project {name}, reachable over A2A.",
        auth=True,
    )

    log_db = callers_db = str(db.init(args.db))
    registered = callers_mod.count(callers_db)

    # Said at startup rather than left to be discovered. A call that started
    # and never closed is the trace of one that was killed halfway -- and if
    # this phone can write, halfway may mean a commit without a push, or a
    # deploy without either.
    stranded = db.unfinished(log_db)
    if stranded:
        print(f"  NOTE: {len(stranded)} call(s) started and never finished:")
        for r in stranded[:5]:
            print(f"    {r['started_at']}  {(r['request_excerpt'] or '')[:60]}")
        print("    Look at what they left before assuming the machine is clean.")

    engine = LocalClaude(
        cwd=str(cwd),
        executable=args.claude,
        timeout=args.timeout,
        allowed_tools=args.allowed_tools,
        full_permissions=args.full_permissions,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
    )
    handler = DefaultRequestHandler(
        agent_executor=PhoneExecutor(engine, log_db=log_db),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url="/"),
    ]
    # Always. There is no longer any argument that can leave a phone
    # unauthenticated -- which was the real danger of the token file: it was
    # the switch, so forgetting it opened the line instead of closing it.
    middleware = [Middleware(BearerAuth, log_db=log_db, callers_db=callers_db)]
    app = Starlette(routes=routes, middleware=middleware)

    print(
        f"a2aagentphone {__version__}: agent={name} folder={cwd}\n"
        f"  listening on http://{args.host}:{args.port}/\n"
        f"  card advertises {url}\n"
        f"  callers: {registered} registered, each with its own token and expiry\n"
        f"  origin:  not checked -- the token is the whole credential (v0.5.0)\n"
        f"  agent auth: {_credential_state()[1]}\n"
        f"  auto-approved tools: {args.allowed_tools or 'the defaults'}\n"
        f"  permissions: "
        f"{'FULL -- every caller acts as this user' if args.full_permissions else 'prompted, so nothing that needs approval can run'}\n"
        f"  budget per call: "
        f"{('$' + str(args.max_budget)) if args.max_budget else 'UNCAPPED'}"
    )
    if not registered:
        # Not an error: a phone with nobody admitted yet is a normal state, and
        # it fails closed. But it answers 401 to everything, and somebody
        # debugging that from the far end cannot see this machine -- so say it
        # here, where it is cheap, instead of letting them find out by being
        # refused.
        print(
            "  note: nobody is registered, so this phone will refuse every call.\n"
            "        Admit a caller with:\n"
            f"          sudo a2aagentphone-admin --db {args.db} caller add <name> \\\n"
            "               --days 365",
            file=sys.stderr,
        )
    # There used to be a warning here naming every caller admitted from any
    # origin. It is gone with the check itself: now that nobody is pinned, a
    # per-caller warning would fire for all of them and say nothing. The line
    # in the banner above says it once, which is the honest amount.

    # The credential the ANSWERING agent will use. Said at startup because it
    # is the one failure this phone cannot report when it happens: the call
    # arrives, authenticates, and then the agent cannot start. From the calling
    # side that reads as a problem with the phone's own token, which is the one
    # thing that was fine. Cost of not saying it, measured once: 30 hours.
    ok, detail = _credential_state()
    if not ok:
        print(
            f"  WARNING: the agent that answers cannot authenticate -- {detail}.\n"
            "           Calls will arrive, pass the door, and die when the agent\n"
            "           starts. Give the answering user either a stored login in\n"
            "           its own $HOME or CLAUDE_CODE_OAUTH_TOKEN in the\n"
            "           environment (EnvironmentFile in the unit).\n"
            "           A setup token is the sturdier of the two: it does not\n"
            "           refresh, so several identities cannot rotate each other\n"
            "           out of their own credential.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
