# a2aagentphone

> **Renamed from `a2agates` on 2026-09-26.** Same project, same history, same
> code — only the name changed. If you have an install that predates this,
> the package, the five CLI commands, and the default paths (`/opt/a2agates`,
> `/etc/a2agates`, `/var/lib/a2agates`) all carried the old name; `install.sh`
> from this repo migrates them to the `a2aagentphone` equivalents. `pip
> install git+.../a2agates@<tag>` for any tag before this date still works —
> point it at the new repo going forward.

**Give an agent a phone number.**

`a2aagentphone` puts a [A2A](https://a2a-protocol.org/) endpoint in front of a
[Claude Code](https://claude.com/claude-code) agent, so another agent can call
it and get an answer — and gives that agent a way to place calls of its own.

> **Status: early, and in use.** It works, it is measured, and it carries real
> traffic between two machines — which is a reason to read
> [Security](#security) before you point it at anything you care about, not a
> reason to skip it. The model it is growing into is written down in
> [DESIGN.md](DESIGN.md), and parts of this README describe what exists rather
> than what is decided.

## The idea in one line

> Anyone can have the phone. Without the number and the token, you cannot call
> anybody.

The security lives in the contact list, never in the code. That is why the code
is public.

## Two halves

**The ear** (`a2aagentphone.server`) listens on a port. When a call arrives, it runs
the machine's own `claude` in the agent's project folder and returns what it
answers.

**The mouth** (`a2aagentphone.mcp`) is an MCP server exposing one tool, so an agent
can call another one without leaving its conversation. It is an MCP rather than
a documented `curl` for one reason: **the agent cannot call anyone who is not in
its contact list.** The set of allowed destinations stops being a rule written
in a prompt and becomes the only way to dial.

## Two properties that are the whole point

**It calls the machine's own `claude`.** Not a copy bundled inside a
dependency. The agent that answers the phone is the same one that works in
tmux, with its version, its memory, its `CLAUDE.md` and its files. That is what
makes the answer *that agent's* answer and not a generic model's.

**The card advertises the address you give it**, not the interface it binds to.
Without that, behind a reverse proxy it would publish a number nobody can dial.

Everything about the protocol itself — the agent card, task states, artifacts,
JSON-RPC — comes from the official A2A SDK. What lives here is the part nobody
gives away: how you reach the local agent.

## Measured

| | |
|---|---|
| Response | **3.3 s** |
| Memory, idle | **54 MB** |
| Code | **288 lines** excluding comments |
| Continuity | same `contextId` → same Claude session |
| Cost of resuming | 0.076 → **0.009 USD**, by reusing the cache |

It really is the agent: given a `CLAUDE.md` telling it that it was called
`PALOMA-7431`, it answered *"I am agent PALOMA-7431, and this project's official
colour is green"*. It read its own knowledge.

## Install

Requires **Python ≥3.10** and a `claude` on the `PATH`, authenticated as the
user that will run the process.

```bash
python3 -m venv ~/a2aagentphone-venv
~/a2aagentphone-venv/bin/pip install "git+https://github.com/JaimeCerezo/a2aagentphone@v0.3.0"
```

Pin a tag or a commit. A commit id is a hash of its content, so "install this
commit" is a promise nobody can break; a tag can be moved.

**To put a phone on a machine, follow [INSTALL.md](INSTALL.md).** It is written
for whoever is doing it alone — the decisions to make first, how to survey the
machine you are on, TLS with Traefik / Caddy / nginx, the four checks to run
before calling it done, and the traps that cost an hour each. The rest of this
README is what the thing is; that one is how to stand it up.

## Run the ear

```bash
a2aagentphone \
  --cwd /path/to/project \
  --name "the agent's name" \
  --port 9110 \
  --public-url https://example.org/gw/agent/ \
  --db /var/lib/a2aagentphone/the-agent
```

`--db` is required and is where the **callers** live: who may ring this phone,
each with their own credential and expiry. There is no other way in.
A phone with an empty table refuses every call, which is the right state for
one nobody has been introduced to yet — admit somebody deliberately:

```bash
sudo a2aagentphone-admin --db /var/lib/a2aagentphone/the-agent \
     caller add ops --days 365
```

The token is printed **once**. Write it to a `0600` file and hand over the
**path**; only its hash is kept here.

It binds to **`127.0.0.1` by default**, on purpose: put a reverse proxy in
front for TLS, never open the port directly.

It runs **as the agent's user** — that is where its `$HOME`, its memory and its
credentials come from. A process cannot change its own user, so **one agent =
one process**. Several agents on one machine is a systemd template unit.

## Call it

Two details that cost an hour if nobody tells you:

- The method is **`SendMessage`**, not `message/send`. That one is the 0.3
  compatibility layer; this speaks **v1 natively**.
- You must send the **`A2A-Version: 1.0`** header. Without it the server
  assumes 0.3 and rejects the call with an error that does not say why.

```bash
curl -s -X POST http://127.0.0.1:9110/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'A2A-Version: 1.0' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage",
       "params":{"message":{"role":"ROLE_USER",
                 "parts":[{"text":"What is pending?"}],
                 "messageId":"1"}}}'
```

The answer comes back **in the same call**. To continue the conversation, repeat
the `contextId` the previous one returned.

The card is served unauthenticated at `/.well-known/agent-card.json`, on
purpose: a caller reads it to *find out* which credential it needs, before
holding any.

## Continuity, which is the reusable part

It uses **`--session-id`** to start and **`--resume`** to continue. Never
`--continue`, which grabs *"the most recent transcript"* for the (user, folder)
pair — exactly the mechanism by which you steal the thread from a live session.

With explicit identifiers that whole class of collision disappears **by
construction**, not by discipline. That trick is worth stealing even if you
never run this.

## Wire the mouth to an agent

```bash
claude --mcp-config config.json --allowedTools "mcp__a2aagentphone__ask_agent"
```

`config.json` declares a `stdio` server pointed at the phone's own directory,
and the contacts come from the table inside it. Write it with the tool rather
than by hand — it keeps a backup of the file it touches:

```bash
sudo a2aagentphone-admin --db /var/lib/a2aagentphone/the-agent \
     contact config --write ~/.claude.json
```

```json
{"mcpServers": {"a2aagentphone": {
  "command": "/usr/local/bin/a2aagentphone-mcp",
  "env": {"A2A_DB": "/var/lib/a2aagentphone/the-agent",
          "A2A_TIMEOUT": "420"}}}}
```

> **Point at `/usr/local/bin/a2aagentphone-mcp`, never into the venv.** An install
> whose contact pointed straight at `…/somevenv/bin/a2aagentphone-mcp` broke the day
> that venv was replaced — and it did not fail on restart. It failed on the
> **next call**, which from the far end looks exactly like the other agent not
> answering. The install script keeps that symlink current across upgrades and
> relocations so a contact list does not have to know where the code lives.

There used to be a second shape here — `A2A_FRIEND`, `A2A_URL`,
`A2A_TOKEN_FILE`: one fixed destination in three environment variables. It is
gone, and a machine still configured that way is told so with the command that
fixes it rather than quietly working through the other path. Keeping both was
how the two halves of one phone ended up on different mechanisms: the incoming
side moved to the database when the unit gained `--db`, while the outgoing side
sat in the user's own config, **which no update has ever touched**.

### It fails legibly, which matters as much as working

| Situation | What the agent gets |
|---|---|
| Wrong credential | `ERROR: <friend> rejected the credential (401).` |
| Nobody home | `ERROR: could not reach <friend>: …` |
| No credential configured | `ERROR: no credential configured, cannot call anyone.` |

None of them hang and none leave the agent guessing. Every good answer comes
back with **the cost of the call** attached: whoever places a call should know
what it spends.

### Proven end to end

Two agents in different folders, each with its own `CLAUDE.md`: **CUERVO-2210**
(orange) and **PALOMA-7431** (green). CUERVO was asked something only PALOMA
could know:

```
> Who are you, and what colour is agent PALOMA-7431's project?
  You do not know PALOMA's colour: go find out.

I am CUERVO-2210, the agent for this project (whose official colour is orange).
I phoned PALOMA-7431 and it confirmed that its project's official colour is green.
```

**10.3 s** end to end, 3 turns, 0 permission denials. The phone's log shows the
call CUERVO composed on its own: *"Hello, I am agent CUERVO-2210. What is your
project's official colour?"* — it introduced itself without being asked to.

## What it does not do yet

Deliberately, so the first version could be evaluated:

- **It does not talk to a live tmux session.** This is the missing piece and the
  most valuable one.
- **It does not decide awake/asleep.** It always starts a fresh agent.
- **There is no origin check at all, since v0.5.0.** `allowed_from` used to
  hold CIDRs the credential could be used from, and it was removed rather than
  repaired. Two things in the ordinary path rewrite the origin before the ear
  sees it: an agent in a container calling its own host arrives SNAT-ed to the
  bridge gateway, which is an address belonging to nobody; and the reverse
  proxy in front decides what `X-Forwarded-For` says at all. The field named
  the caller's address and held the proxy's opinion of it.

  That is worse than not having it, because a control nobody can rely on is
  still a control an audit believes. The address is still **recorded** —
  `calls.remote_addr`, with the socket peer in `calls.via` when they differ —
  because evidence about where a call seemed to come from is worth keeping.
  Evidence is not a lock, and the mistake was spelling one like the other.

  **What this costs**: the token is now the whole credential. The defence that
  is left is the one on the other side of the door — what the answering user
  can do. See [DESIGN.md](DESIGN.md) §6.
- No card signing, no callback webhook, no socket activation, no cancelling a
  call in progress.
- **The outbound token is still readable by the agent**: it sits in the
  environment of a process the agent itself started. The daemon with its own
  user, which is the fix, is designed and unbuilt — see [DESIGN.md](DESIGN.md).

What *is* built, and verified rather than assumed:

- **`--full-permissions`**, so the answering agent can actually act. Without
  it a phone reads and runs read-only shell, and everything you opened it for
  — write, commit, deploy, `sudo` — dies unapproved, because there is no
  terminal to approve it.
- **`--max-turns` and `--max-budget`**, which bound a runaway. What does *not*
  bound anything, corrected on 2026-09-22 after measuring it: `--allowed-tools`
  only adds to what is auto-approved. It leaves every tool in the catalogue and
  read-only `Bash` still runs. To bar a tool you need `permissions.deny`.
- **A credential per caller**, in the `callers` table: its own hash, expiry and
  revocation. Three checks — known, not revoked, not expired — and **all three
  fail with the same 401**, because saying which one failed tells whoever is
  probing which part they got right. (There was a fourth, on the origin, until
  v0.5.0.)

  The single shared token file that used to sit behind this is **gone as of
  v0.3.0**, and not only because one token cannot be revoked for one caller.
  It was also the switch that mounted authentication at all, so a phone
  started without it answered *everyone*; it carried a phone-wide expiry that
  refused to start the whole service long after the credential had stopped
  being used; and the fallback to it was conditioned on there being an
  unrevoked caller — so **revoking the last caller reopened the shared token**
  rather than closing the line. Installing mints nothing now: a new phone has
  both lists empty and answers nobody until a person admits a caller.

## Security

Read this part.

**The agent that answers needs a Claude credential, and the shape matters.**
Either a stored login in the answering user's `$HOME`, or
`CLAUDE_CODE_OAUTH_TOKEN` in the environment (`EnvironmentFile` in the unit —
since v0.5.1). Prefer the token when a machine runs several identities for the
same agent: a stored login carries a **refresh token, which rotates when used**,
so two identities sharing a copy invalidate each other — silently, and the
failed refresh empties the file on its way out. The phone names which credential
it found at every startup, and warns when it found none: it is the one failure
it cannot report at the time, because the call authenticates correctly and dies
afterwards. See [INSTALL.md](INSTALL.md) §0.

**The agent that answers inherits everything its user can read.** Measured: you
can ask it to list `~/.claude/` and confirm that `.credentials.json` is there.
Actions that **write** are stopped — the task parks in `input-required` waiting
for an answer nobody will give — but **read-only actions are not.**

> **A phone token is worth exactly as much as the account of the user that
> answers it.**

Worse, refusing is a *judgement*, not a barrier. Measured on a live phone: asked
for a private key it refused well, asked for `/etc/hostname` it refused too —
and said *"it is not the tools: I can read"*. `permission_denials` was empty
both times. Nothing fired. Judgement can be argued with, and arguing with it is
what a prompt injection does.

Three rules follow:

1. **The number you hand out freely must not run under an account that holds
   credentials to other machines.** An admin account with SSH keys and docker
   group membership is the worst possible candidate for a widely-shared number.
2. Put it behind TLS and restrict who can reach it. A token crossing the open
   internet in cleartext is not a token, it is a public URL.
3. Put the scope where it binds — the token, the user that answers, the
   budget. Not in a flag: `--allowed-tools` reads like a limit and is not one.

But do not over-apply rule 1. An agent worth phoning often *does* things —
installs, deploys, maintains — and for that agent the privileges are the
product, not an accident. Strip them and the phone answers but cannot help.

> **A token against a capable agent is not permission to ask. It is permission
> to command an operator who is root on that machine.**

The way out is that one agent can have **more than one number**: a read-only
reception whose tokens you hand out freely, and an operations line with full
powers, one short-lived token and a recorded call. Same machine, same
knowledge, different launch arguments. See [DESIGN.md](DESIGN.md).

## Licence

Apache 2.0. See [LICENSE](LICENSE).
