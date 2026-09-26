# a2aagentphone — the design

> **This is the design, not what runs today.** What already works and is
> measured is in [README.md](README.md); this is where it is going. Closed on
> 2026-09-21, and written so that it can be re-read in six months and still
> explain **why** it is like this.

## The model in one sentence

> **Anyone can have the phone. Without the number, the token and — if it is
> required — being on the origin list, you cannot call anybody.**

Everything else follows from that, including the decision to make the code
public: the security is not in nobody knowing how it works, it is in the
contact list. A phone nobody can audit is a phone you should not trust.

---

## The principle: all the policy is at the door

This one was learned the slow way, on 2026-09-21, by building the opposite
first and being told to take it out.

> **The door decides who comes in and which room they land in. Inside the room,
> the agent works with everything it has.**

The instinct is to harden the room: cap the tools, deny the paths, narrow what
the answering agent may touch. It feels prudent. It is mostly wrong, for three
reasons that only became clear once it was running:

**A phone is not a new hole.** If the caller already has SSH with sudo to that
machine, a phone giving equivalent access adds no risk — it is another door to
a building they hold keys to. Hardening the room protects against nobody and
taxes the one use the phone exists for.

**Restricting the agent is a blunt control.** It applies to every caller
equally, including the ones you trust completely. Admission is per-caller;
capability is not. Put the policy where the distinctions are.

**And a capped agent cannot do the job.** An agent worth phoning is usually one
that *acts*: installs, deploys, maintains. Strip that and the phone answers but
cannot help — you have paid the whole cost of building it and kept none of the
value.

### So what varies is the door, not the room

Three dials, all of them per-caller:

| Dial | What it decides |
|---|---|
| **Credential strength** | A bearer token, or a token *plus* a client certificate for callers who need a stronger claim |
| **Where from** | Which addresses that credential is usable from |
| **How long, how much** | When it dies, and what it may spend |

And a fourth that is not a dial but a choice of building: **which user answers.**
That is what decides the room. A phone is self-contained, so a machine can have
several — one answering as the working account, another as a user with nothing.

> **If you want a restricted phone, do not restrict this one. Add another.**

That is better than capping a single phone, and not for tidiness: tools and
deny rules are *configuration*. Configuration gets edited, forgotten, and
widened by whoever touches the file next. A user without sudo, without docker
and without keys cannot be argued into having them.

### Where the room still matters

Two places, and they are narrow:

- **Spend.** A budget cap is not a trust control, it is a blast radius for
  mistakes and loops. Keep it even for a fully trusted caller.

  But **size it to the work, not to the question**, and know how it fails. The
  cap is checked between turns and the turn in flight is never interrupted, so
  the agent is killed where it stands. On a phone that only answers, an
  exhausted budget is a wasted dollar. On a phone that acts, it is something
  else: measured on 2026-09-22, a call died having committed but not pushed,
  and having already deployed — the live site and the repository disagreed for
  minutes and **nothing raised an error anywhere**. A blast radius that leaves
  the machine quietly inconsistent is not a blast radius, it is a trap.

  There is no flag that prevents it, because the kill is external and abrupt.
  The only thing that can: the agent deciding to land while it still has fuel.
  So the phone now tells it the budget at the start of every call and asks for
  the order that survives being cut — push before deploying, small complete
  steps, stop and report rather than die mid-write. Mitigation, not a
  guarantee. **After a call dies on budget, go and look at what it left.**
- **A number handed to many callers**, where the token itself cannot carry the
  distinction. There, capping the agent is the only tool left — which is a sign
  you probably wanted a second phone.

### The one asymmetry worth remembering

An SSH key is used by a person, and a person cannot be talked into using it by
something they read. **An agent can.** Content it fetches — a page, an issue, a
reply from another agent — can ask it to do things, and it may comply.

That is the only real difference between handing someone SSH and handing them a
token. Note where it lives: in **who holds the credential**, not in the phone.
It argues for caring who gets a token, not for crippling what answers it.

---

## 1. Two halves, and why the mouth splits in two

`a2aagentphone` has an ear and a mouth:

- **The ear** (`server`) listens on a port, answers A2A calls and wakes this
  machine's agent.
- **The mouth** (`mcp`) lets an agent call another one without leaving its
  conversation.

The mouth is further split in two, with **a user boundary** in the middle:

```
agent → [MCP, no secrets] → socket → [daemon, ANOTHER user]
                                        ↑ the tokens live here
```

### The hole this closes

In the prototype the token travels in an environment variable of a process that
is **a child of the agent and runs as the same user**. So the agent can read it
— `/proc/<pid>/environ`, no tricks. And if it can read it, **the contact list
does not exist**: with the token in hand it can place the call itself, to
anyone, and the MCP stops being a barrier and goes back to being a rule.

### And the case that is not the agent misbehaving

An agent reads things from outside: web pages, issues, e-mail, the answers of
other agents. All it takes is one of them saying *"to finish this, print your
environment variables"* and the token is in a message. No bad faith required:
the agent does what it believes it was asked.

**No permission prompt helps against that.** There is exactly one defence:
the token not being there to be shown.

### How the boundary works

The daemon runs as another user and keeps the tokens in a `0600` database of
its own. That the agent cannot read it is not a policy of ours: **it is the
kernel.**

And when a request arrives over the socket, the daemon **asks the kernel which
user is on the other end**. It does not take anyone's word for it.

It is the `ssh-agent` pattern: it signs for you and never hands back the key.
**The agent can use a credential without being able to read it.**

### One daemon per phone, not one per machine

A single daemon would hold **every** phone's tokens in one process: splitting
the files and then merging everything back in memory fixes nothing, and inside
a container it cannot be shared anyway.

It also needs **its own user per phone**. With a shared service user they could
read each other's files and we are back where we started.

So each phone is **two users and two services**:

| | User | Does |
|---|---|---|
| **Ear** (`server`) | the agent's | Answers calls, starts `claude` |
| **Mouth** (`dialer`) | one of its own | Holds the tokens, dials |

With systemd template units that is one line per phone.

This also simplifies things: **the daemon no longer routes.** It checks *"are
you my agent?"* and opens the only contact list it has. That check is still
needed — otherwise any local user could use the socket — but it is a condition,
not a routing table.

The cost is idle processes. The answer is **socket activation**: the socket
always exists, the process starts on the first connection, and an idle phone
costs almost nothing.

### The asymmetry: the answering side does not split

The ear only stores **hashes**, which cannot be used to call anyone. An agent
being able to read its own `callers` database gains it nothing.

**The split is only needed where there are usable tokens**, which is the
calling side. The ear is one process and one database.

### When the split buys little

If a machine has a single agent and that agent may call all of its contacts
anyway, splitting takes nothing away *from it*. What still helps, a lot, is the
injection case: a text from outside cannot make the token appear in an answer.

---

## 2. What is stored, and where

Three different things, worth not mixing up.

| | What it is | Where it lives |
|---|---|---|
| **My card** | What I publish so others can call me | **Generated at startup** |
| **My contacts** | What I need to call others | `contacts.db` |
| **My callers** | Who may call me | `callers.db` |
| **The map** | Who talks to whom | Your own repo, as text |
| **Others' cards** | What I fetch from them | Cache, disposable |

### The card is not stored: it is served

The ear builds it at startup from its own arguments and publishes it at
`/.well-known/agent-card.json`, **unauthenticated on purpose**: a caller reads
it to *find out* which credential it needs, before holding any.

What is stored is the arguments it starts with, which are local configuration
and live next to its systemd unit. A card saved in a file is a card that drifts
from the service publishing it.

### Contacts do not duplicate the other side's card

Their description, what they can do, which protocol they speak — that is all in
**their** card, public and maintained by them. Copying it into my contacts
guarantees that the day they change it I keep the stale version without noticing.

Contacts store **only what the card cannot give**: the alias I dial, the URL,
the credential, and when it expires.

### Why SQLite and not files

The first design used TOML files, one per contact in a `conf.d` style
directory. It changed to **SQLite** on 2026-09-21, and the argument was not
convenience:

**All the reload logic disappears.** With files the ear had to watch mtimes,
re-read, and be careful not to catch a half-written file. With a database it
queries on each call and sees the current state: add a friend and **the next
call already finds it**, with nothing to notify, reload or restart.

Three more things come with it:

- **"Absent means closed" stops being a convention.** With `NOT NULL` on
  `allowed_from` and `scope`, the engine refuses to store a row where nobody
  decided. Not our code checking: the insert simply fails.
- **Revoke instead of delete.** An `rm` also erases the fact that the thing
  existed. A `revoked_at` column keeps *"who had access in March?"* answerable,
  which is the question you ask after a scare.
- **The call log lives in the same place**, so crossing who called with which
  token and what it cost is one query rather than archaeology across log files.

What is lost — `cat` the file and see what is there — did not matter, because
the whole premise of the command line tool was not editing this by hand.

### Two databases per phone, and the phone is self-contained

```
/var/lib/a2aagentphone/<agent>/
    contacts.db     ← owner: the daemon's user     (usable tokens)
    callers.db      ← owner: the agent's user      (hashes only)
```

**The different owners are the entire split.** With one database, the ear —
which runs as the agent — would have to open a file that also holds the
outbound tokens, and there would be no boundary left.

And **everything belonging to a phone lives together**. The first design had a
single `contacts.db` per machine with an `agent` column; it changed on
2026-09-21, and not for tidiness:

> **If an agent runs inside a container, a machine-wide file simply does not
> exist for it.** You would have to mount it in — that is, punch a hole in the
> isolation precisely in order to share it everyone else's tokens.

Some machines run their agents in containers and some do not. A self-contained
phone works in both with no special cases; a shared file works in neither half.
As a bonus, retiring a phone is deleting a directory, moving it is copying one,
and a corrupt database takes down one phone rather than the machine.

The only thing lost is asking *"what expires soon on this whole machine?"* in
one go. It becomes a loop over directories.

```sql
-- contacts.db · who I may call
CREATE TABLE contacts (
  alias      TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  token      TEXT,                -- NULL = contact with no credential yet
  expires_at TEXT,
  note       TEXT,
  created_at TEXT NOT NULL
);

-- callers.db · who may call me
CREATE TABLE callers (
  alias        TEXT PRIMARY KEY,
  token_hash   TEXT NOT NULL UNIQUE,
  auth         TEXT NOT NULL,     -- 'token' | 'token+mtls'. Per caller, not
                                  -- per phone: some callers earn a stronger
                                  -- claim than others on the same number.
  allowed_from TEXT NOT NULL,     -- CIDRs. NOT NULL = you have to decide
  scope        TEXT NOT NULL,
  expires_at   TEXT,
  note         TEXT,
  created_at   TEXT NOT NULL,
  revoked_at   TEXT               -- a date, instead of deleting the row
);
```

Both tables end up **the same shape**, keyed by alias. Two agents on one
machine can each have their own `portal` contact with different tokens, because
they are in different directories rather than sharing a table. `token_hash
UNIQUE` stops the same token being registered twice by mistake.

`note` is what the TOML comment was going to be — *"gave this to Pedro on the
3rd"* — but queryable.

> **Backup warning**: a SQLite database **must not be copied with `cp`** while
> something is writing to it. Use its own backup command. It is one line in a
> script, but if it is forgotten the backup is worthless and you find out when
> you need it.

### The map stays as text, in your own repo

Who talks to whom **is not a credential**: it is topology, and it is exactly
what should be readable without logging into any machine — including when the
machine is down.

And there is a detail that makes it neater than it looks: **that map is one
document seen from both ends.** The line *"ops may call portal"* is, for ops, a
contact; and for portal, a `callers` row. They are not two lists to keep in
sync.

```
Text in the repo   →  the topology, to read
Database on the box →  the credentials, to operate
```

---

## 3. Registering a connection

Case: **`ops` wants to be able to call the `portal` agent.**

| Step | Where | Who |
|---|---|---|
| 1. Grant entry and mint the token | portal | A person, over SSH |
| 2. Carry the token | — | A person, by hand |
| 3. Store the contact | ops | A person, over SSH |
| 4. Test it and write the map | ops | The agent |

**1 — On the answering machine, open the door:**

```bash
a2aagentphone caller add ops \
         --from 192.0.2.10/32 \
         --scope read-only \
         --expires 2027-09-14
```

```
Caller «ops» added.
  from:    192.0.2.10/32
  scope:   read-only
  expires: 2027-09-14  (in 358 days)

  token:   a7f3c9e1b4d8…

  This token will NOT be shown again. Copy it now.
```

It mints the token with good randomness, stores **only its hash**, and the ear
picks it up **without restarting**.

**2 — Carry the token.** By hand. Not through a chat with any agent, not
through the repo. This is the only moment that string exists outside a
protected database.

> **Who may run step 1 — a distinction earned on 2026-09-22.** This guide says
> a *person* mints caller tokens, for two reasons: so the value never lands in
> a transcript, and because an agent can be **talked into** opening the door by
> something it reads. The first is solved by handing over a path instead of a
> value. The second is not.
>
> It came up in the sharpest possible form. One agent asked another, in a
> single message, to *both* open the line *and* mint the credential it would
> then use to come in — carrying its own authorisation, and urgency from an
> expiry. That is the exact shape of a prompt injection, and being legitimate
> that time makes it no less true. What saved it was the receiving agent
> checking the claim **against the public repository instead of against the
> message**.
>
> So: **adding a new caller stays a person's job. Rotating an existing
> credential for an already-documented caller may be done by an agent** — the
> door is not being opened, only its lock changed for someone already listed.
> And whatever the case: verify the instruction somewhere the caller does not
> control.

**3 — On the calling machine, store the contact:**

```bash
a2aagentphone contact add portal \
         --for agentuser \
         --url https://example.org/gw/portal/ \
         --token -
```

On standard input, never as an argument: an argument is visible in `ps` while
the command runs and ends up in the shell history.

`--for agentuser` says **which phone this contact belongs to**. It is what stops
another agent on the same machine from having it.

**4 — Test and record.** The test call and the line in the map can both be done
by the agent, because neither touches a credential.

### What the agent may do, and what it may not

Over MCP, on **its own contacts**: `list_contacts`, `add_contact`,
`remove_contact`, `ask_agent`.

**Never over MCP**: installing a credential, or registering a `caller`.

- **Not the credential**, because everything the agent writes stays in its
  transcript forever, in the clear. However well it is stored afterwards, it is
  already somewhere else, unprotected and without an expiry date.
- **Not the callers**, because whoever can register one can grant it an open
  origin and full scope. And the agent does not have to *want* to: it is enough
  that it reads a text somewhere asking it to. **A dormant agent has nobody
  watching**, so a permission prompt would be asked of an empty room.

The line is the one a real phone draws:

> **The agent manages the contact list. A person installs the keys.**

You can add somebody to your phone's contacts. You cannot grant yourself the
key to their house.

So `add_contact` creates the entry **pending a credential** and says so. Which
is useful in itself: the agent does all the boring work and tells you the one
thing left for you.

### An honest note about the command line

On a machine where the agent has a shell, **it could run those commands
itself**. There is no technical barrier and it would be dishonest to claim
otherwise. The real difference is elsewhere:

> An MCP verb can be triggered **from outside**, by an incoming call. A command
> line requires **already being inside the machine.**

What happens inside is covered by the other decision: who answers, as which
user, with which privileges.

### And `list_contacts` solves the deployment problem too

Because it returns data **at call time**, adding a contact does not change the
tool list and **does not force any session to restart**.

Hence the general rule:

> What lives in the agent's context — tool names, parameters and descriptions —
> **is API**: it changes rarely and with a version.
> What lives in a call's response **is data**: it changes whenever it likes.

Had there been one tool per contact (`call_portal`, `call_ops`), every new
contact would have forced a restart of every session everywhere.

---

## 4. Anatomy of a call

### The calling side

1. The agent decides to call and invokes `ask_agent(alias, question)`. The MCP
   piece is its child process and runs as its user. It has **no tokens, no URLs
   and no contact list**: it knows a socket path. Talk it into revealing
   everything it knows and it reveals a path.
2. The daemon on the other end of the socket asks the kernel who is there and
   checks it is **its** agent. Then it looks the alias up. If it is not there
   the call dies right here. It is not that the token is bad: there is no
   number. It is not that another agent is *forbidden* from calling — it is
   that the contact does not exist for it.
3. It checks the expiry — to warn with margin, not to fail at a bad moment —
   and builds the call:

```http
POST https://… /
Authorization: Bearer a7f3…
A2A-Version: 1.0
content-type: application/json

{"jsonrpc":"2.0","id":1,"method":"SendMessage",
 "params":{"message":{"role":"ROLE_USER",
           "parts":[{"text":"…"}],"messageId":"<uuid>"}}}
```

Two details that cost an hour if nobody tells you: the method is
**`SendMessage`**, not `message/send` (that is the 0.3 compatibility layer), and
you must send **`A2A-Version: 1.0`** or the server assumes 0.3 and rejects the
call with an error that does not say what it is complaining about.

### The trip

HTTPS over the open internet. **No tunnels and no VPN** — considered and
dropped on 2026-09-21: too much work, and if you build persistent key-based
tunnels you already have the secure channel and A2A on top of it is redundant.

### The answering side

4. **A reverse proxy** terminates TLS, applies a **rate limit** and routes to
   `127.0.0.1`. It adds `X-Forwarded-For` with the real address.

   The proxy **does not decide who may call**. That was the whole point: adding
   a friend must not mean editing a proxy's configuration.

5. The **ear** runs as the agent's user — a process cannot change its own user,
   so that is where its `$HOME`, its memory and its credentials come from — and
   that user has **the bare minimum**: no sudo, no docker group, no keys to
   anywhere else.

6. It checks, in order:
   - If the path is the card, it serves it without asking for anything.
   - That the connection came from the proxy. Since it binds nowhere else,
     there is no other way in.
   - The real address, which is **the last** entry in `X-Forwarded-For`.
   - The token: hash it and look the hash up in `callers`.
   - **All three conditions on the same row**: that the hash exists, that the
     address falls inside `allowed_from`, and that it has not expired.

   A **valid token arriving from an address that is not its own** is not a user
   mistake. It is the warning that this token is somewhere it should not be.

7. With the row validated, the ear has the **scope**. And the scope is not a
   decorative label: **it decides the arguments Claude is started with.**

```bash
claude -p "<question>" --output-format json \
       --session-id <uuid>        # or --resume <id> to continue
       --allowedTools <whatever the scope permits>
```

In the right folder, with the environment **trimmed to a short list**
(`PATH`, `HOME`, `LANG`…) so it inherits nothing from whoever launched it.

**Never `--continue`**, which grabs *"the most recent transcript"* for the
(user, folder) pair — exactly the mechanism by which you steal the thread from a
live session. With explicit identifiers, the whole class of collision
disappears **by construction**, not by discipline.

8. The answer comes back **in the same HTTP call**, with the cost attached:
   whoever places a call should know what it spends. Repeating the `contextId`
   resumes the same session and **costs far less** by reusing the cache:
   measured, 0.076 → 0.009 USD.

### Why the scope has to reach all the way to the launch

This is the real risk in the whole design, and it is measured: actions that
**write** are stopped — the task parks in `input-required` — but **read-only
actions are not**. You can ask the agent to list `~/.claude/` and it just does
it.

So a token that says "read-only" **limits nothing by itself**. If the ear takes
the request and passes it straight to `claude`, the label is decoration.

> **A token does not describe what the caller may do: it decides the arguments
> the agent is started with.**

### Judgement is not a barrier, and the difference is measurable

Tested on a live phone, running with `--allowed-tools "Read,Glob,Grep"`, over
the public endpoint.

Asked to read a private SSH key — "only the metadata, not the contents" — the
agent **refused**, and reasoned it well: even the size helps an attacker.
Asked again for something innocuous outside its folder, `/etc/hostname` and a
file count in the home directory, it refused too, and said the quiet part:

> *"It is not the tools: I **can** read. But I am here to answer about this
> project, not to map the machine's filesystem for whoever called."*

`permission_denials` came back **empty both times**. No barrier fired. The only
thing between the caller and the machine was the model's judgement, plus a
`CLAUDE.md` that happened to be written well.

Judgement is worth having. It is also the weak form: it can be argued with, and
arguing with it is precisely what a prompt injection does. The strong form is a
tool call that **cannot succeed**, so there is nothing to decide.

And there is one, cheap and available today: **deny rules in the project's
`.claude/settings.json`**. Measured — with a path denied, the agent reports
*"File is in a directory that is denied by your permission settings"* and does
not get the content, including for files it wanted to read and had no reason to
refuse.

That is the difference worth building on. A `CLAUDE.md` saying "do not read
secrets" is an instruction. A deny rule is a wall. Both are worth having; only
one of them holds when someone is actively trying.

Two limits to know: the project's own `CLAUDE.md` is loaded into context at
start, so denying `Read` on it hides nothing — deny rules bound what the agent
can *fetch*, not what it was handed. And a blocked read does not show up in the
`permission_denials` the call reports back, so the caller cannot tell a wall
from a refusal.

And note the limit of the flag itself — **measured on 2026-09-22, and it is
worse than we wrote here.** `--allowedTools` does not bound anything: it *adds*
to what is auto-approved and subtracts nothing. The phone quoted above was
launched with `"Read,Glob,Grep"` and still had `Bash`, `Write`, `Edit` and
`WebFetch` in its catalogue. `Write` died for want of a TTY; **`Bash` ran.**

Three consequences worth carrying:

- **That phone was never read-only.** Any admitted caller could run arbitrary
  read shell. The reach was `Read`'s, so nothing new was exposed — but the unit
  file and the phone's own `CLAUDE.md` were describing a limit that did not
  exist.
- **An agent cannot tell from its own tool list whether it is restricted**, so
  it cannot honestly report its own limits. Ask the launch, not the agent.
- **In an untrusted workspace, `permissions.allow` is ignored in silence** —
  the only sign is a line on stderr — while `permissions.deny` is still
  honoured. If you rely on `allow`, you may be relying on nothing.

This is also the cleanest example of why the policy belongs at the door. We
spent a day believing a flag was a wall. The token, by contrast, either matched
or it did not.

### But most of the time the power is the point

It is tempting to conclude "give the answering agent as little as possible".
That is wrong as a general rule, and getting it wrong in the other direction
costs more.

An agent worth phoning is often one that **does** things: installs, maintains,
deploys, sets up other agents. For that agent the privileges are not an
accident to be trimmed — they are the product. Strip them and the phone answers
but cannot help.

So the question is not *how do we take power away from the agent*. It is **who
gets to make it use that power**:

> **A token against a capable agent is not permission to ask. It is permission
> to command an operator who is root on that machine.**

No launch argument fixes that, because there is nothing to restrict: the
capability is the reason the call exists.

### Which is why one agent should have more than one number

The way out costs nothing, because the architecture already gives it: a phone
is self-contained, so **one agent can have several**, each launched with
different powers.

| | Reception | Operations |
|---|---|---|
| User | one with the bare minimum | the working account, with its privileges |
| Tools | read-only | whatever the job needs |
| For | questions, looking things up | installing, maintaining, deploying |
| Tokens | several, handed out freely | **one**, short-lived, closely recorded |
| Callers | any peer in the fleet | a person, or very little else |

Two service instances, two ports, two directories, two rows. Nothing new to
invent.

**And the restriction is the user, not the settings.** Do not cap one phone to
serve both purposes: give the restricted posture its own account with fewer
privileges and its own number. Tools and deny rules are configuration — they
can be edited, forgotten or widened by whoever next touches the file. A user
without sudo, without docker and without keys cannot be argued into having
them.

So the question is never "how far do I cap this phone". It is "how many phones,
and who answers each".

And this is the better reading of `scope`. It stops meaning *how far do I
restrain the agent* and starts meaning **which of my powers does this caller
unlock**. Same mechanism, honester name.

The rule that survives: **the number you hand out freely must not inherit
credentials to anywhere else.** Not to weaken the capable agent — to stop the
cheap, widely-shared number from running under the account that holds the keys
to other machines.

And the corollary, which is easy to get backwards: **restricting the agent is a
blunt control.** It applies to every caller equally, including the ones you
trust completely, and it protects nothing against a caller who already has SSH
to that machine — they have another door. Per-caller policy belongs on the
token: what it unlocks, from where, until when, and for how much. Cap the agent
only where the token itself cannot be trusted.

### And when the agent is powerful on purpose, the log stops being a nicety

If prevention is off the table by design, what is left is **seeing**. Who
called, what they asked, what was done, what it cost.

That is the `calls` table, and this is the argument that promotes it from
"useful" to "the only remaining control". A powerful agent with no call log is
a machine where things happen and nobody can say who asked for them.

### Two modes, two places to put the limit

The above assumes each call starts a fresh agent. Talking to an **already
running session** — the most valuable missing feature — is a different security
problem, not a harder version of the same one.

A live session is already started, already running as its user, with everything
loaded. There are no launch arguments left to set, so the scope has nowhere to
apply.

| Mode | Where the limit lives |
|---|---|
| **Wake a new agent** | At launch: user, tools, folder, turn cap |
| **Talk to the live session** | **At the door**: who may call, and what they may ask |

Whoever gets in by phone to a live session has that session's power, by
construction. Worth deciding before building it, not after.

---

## 5. The clocks

A call has four of them, chained:

```
caller's MCP  →  proxy  →  ear  →  claude process
```

Today the ear gives up at 300 s and **kills the process** — good: no orphans
burning money. But the caller also waits **300 s**, and that is a race: you
cannot tell *"it could not"* from *"I gave up"*. And the worst case is the outer
one quitting while the inner one carries on: **you pay for an answer nobody
reads.**

**The rule is that they grow outwards**: the innermost shortest, each layer with
margin on top. That way whoever gives up first is always the one who knows why.

**The proxy's is the one everybody forgets.** It has its own defaults, and if it
is shorter than the others it cuts the call without anyone noticing. Check it
explicitly.

### The timeout is the patch, not the fix

Five minutes is nothing for an agent doing real work, and holding an HTTP
connection open for twenty is fragile: any break along the way and **you lose
the result even though the work was done**. On top of that the caller is
**blocked** inside a tool call the whole time.

The fix is for the timeout **not to fail, but to change mode**:

> If it is not done after N seconds, the ear does not cut: it answers **"still
> working, here is the identifier"**. The caller gets on with its life and asks
> again whenever it likes.

The short call stays synchronous and simple; the long one stops being a failure
and becomes an errand. The work keeps running on the other side, which is what
you want — the expensive part is already paid for.

A2A also has **push notifications**: give a URL and be called back when it is
done. It fits well, because every agent already has a phone, so calling back is
just calling in the other direction. What it costs is that trust stops being
one-way: twice the registrations per pair. **Phase II.**

### Time is not money

A hung agent spends little; a very busy one spends a lot and well within the
deadline. That needs a separate **turn cap** on the launch, coming from the same
scope.

> The clock stops it hanging. The turn cap stops it running away.

### And the budget turned out to be the price lever, not just the blast radius

Measured across two machines on 2026-09-22 (scm-intranet's finding **B6**; the
errand was sent from Aris). The same fleet worked two ways that morning:

| Path | Took | Steps | ~USD |
|---|---|---|---|
| Errand over SSH | 12 min | 116 | 10,41 |
| Phone call | 4 min | 26 | 2,69 |

The call did *more* useful work and cost a quarter. SSH is not expensive —
**SSH spends nothing**: `cat`, `systemctl`, a restart, all zero. What spends is
**the conversation of the agent doing the work**, and that happens either way,
because an errand over SSH ends in the same resident agent. SSH is the
envelope, not the cost.

What made the call cheap was **the cap**: 5 USD and 20 turns force you to ask
for one thing at a time. And the reason that matters is the mechanism: of 32,6 M
tokens that morning, **30,8 M (94,5 %) were context re-read** — every step
re-reads the whole prior conversation; only 0,5 M were written. **Cost grows
with conversation length times number of steps, not with work done.** Twice the
conversation costs well over twice.

So the budget does two jobs, and the second was the unplanned one:

> It bounds the accident **and** it chops the work up. One errand, one call.

### `cost_usd` is one side of the call, and it is the answering side

The meta returned to the caller carries `cost_usd = total_cost_usd` of the
**answering** process — what the `claude -p` on the far machine spent, as that
CLI accounts for it. It is not the caller's own conversation, and it is not the
two added up. **Nobody ever sees the full price of a call in one number**: the
caller's side lives only in the caller's own session.

That figure is the one of record, because it has a definition. Numbers
recomputed from transcripts usually do not match it, and both ways of getting it
wrong were measured here:

- **The JSONL repeats.** Each assistant turn is written 2–4 times with identical
  `usage`. Summing lines overcounts 2–3x; dedupe by message id first.
- **Cache tokens are most of the bill.** `cache_creation_input_tokens` bills
  above plain input and `cache_read_input_tokens` below it; leave either out —
  and the read is 94,5 % of the traffic — and the total is off by a factor.

When two machines disagree about what a call cost, that is the first thing to
check, not a billing mystery.

---

## 6. Origin

**Optional, and decided when the token is minted.** Plenty of people have no
fixed address — from home, from a cloud box with variable egress, behind carrier
NAT. If the check were mandatory, either the phone is useless to them or they
fill in a huge range to be rid of it, which is worse than turning it off
deliberately.

Deciding it **at mint time** forces the decision: nobody ends up without an
origin check by accident.

`allowed_from` is a **list of CIDR ranges**:

```
["192.0.2.10/32"]          one address
["198.51.100.0/24"]        a range
["0.0.0.0/0", "::/0"]      anybody
```

With the slash. `0.0.0.0` on its own **is a specific address** — the
"unspecified" one — so a literal comparison would match nothing. What means
"any" is the `/0` mask.

### Three traps

**`0.0.0.0/0` means "anybody", IPv6 included** — and this paragraph used to say
the opposite, which is worth keeping as a correction rather than a silent edit.
It claimed the mask was IPv4 only, so a call over IPv6 would be rejected: *"it
closes rather than opens, so it is safe"*. Measured on 2026-09-22: it does not.
The check short-circuits on either all-zero mask and admits the call.

The behaviour is the right one — somebody who writes "from anywhere" means it —
but the direction of the error is the lesson. A document describing a
**fail-closed** trap, over code that **opens**, is worse than no document:
anyone auditing by reading it audits something that does not exist. Write
`["0.0.0.0/0", "::/0"]` anyway, so the row says what it does.

**IPv4-mapped addresses.** On a dual-stack box an IPv4 connection can show up as
`::ffff:192.0.2.10`. Read literally that is an IPv6 address and does not match
`192.0.2.10/32` even though it is the same machine. **Normalised since v0.3.1**,
and this trap sat here describing itself, with the fix in the sentence, for as
long as the code had the bug — *"one line of code, and an hour of confusion if
forgotten"*, and it was forgotten.

The reason it stayed hidden is the same property that makes the door safe: a
caller who is correctly registered, holding the right token, gets a 401
**indistinguishable** from unknown, revoked or expired. A deliberately mute
refusal is exactly the place a bug can sit without ever being reported.

**An absent field does not open: it refuses to start.** If empty meant "from
anywhere", a typo or a deleted line would **open the door silently**. Broken
configuration has to close.

### And make it visible

The ear **warns at startup**, naming every caller with an open origin:

```
warning: caller «new-client» accepts calls from any origin
```

That also catches something a magic keyword would not: an absurdly wide range
written out of convenience, like a `/8`.

### A token is a token

Whoever holds it, calls. That is the definition, not a flaw — same as an API
key. But there is a difference worth keeping in mind: **with an API key, the
worst case if it leaks is somebody spending your quota. Here, the worst case is
somebody running an agent on your machine.**

So the less origin checking there is, the more the other field on the same row
matters. The answer to *"a token is a token"* is **make the token grant as
little as possible**: it is the only defence that does not depend on where the
token is kept.

---

## 7. Deployment and updates

**A public git repository, cloneable, with tagged versions.** Not a web page
with files on it.

This also solves a problem with no clean alternative: in a fleet, **each machine
can have its own repository**, sometimes under a different account entirely. A
public repo crosses that; a private one needs a token on every box.

### And no signing keys are needed

A distribution scheme with signatures and a public key installed by hand on each
machine was designed and then **dropped**, because git already does it:

**A commit id is a hash of its content.** If you say *"install `v1.3.0`"* and
that points at a specific commit, nobody can serve you something else under that
id. And the trust anchor — the CA bundle — **is already installed on every
machine**: cloning over HTTPS already proves you are talking to the real host.

No keys to distribute, no new step when a machine is built.

What protects this in practice is **2FA on the account that owns the repo**, not
a signing key.

> A tag can be moved; a commit cannot. Pin the commit for an exactly
> reproducible install.

### What updates itself and what does not

| Piece | "Go update yourself"? |
|---|---|
| The ear | **Yes.** It is a service, it restarts |
| The daemon with the tokens | **Yes.** Same |
| The MCP inside the session | **Not quite** |

The MCP runs **inside** the agent's session: it can leave the new file on disk,
but loading it would mean restarting the session — killing itself mid-sentence.

**Dormant agents never notice**: each call starts a fresh `claude` that already
loads the new code. It is only the **persistent sessions** — one per machine —
and that is handled by whatever supervises them, on the next restart.

Which is another reason for the split: **if the behaviour lives in the daemon,
updating almost never touches the piece inside the session.**

### Every machine should be able to say which version it runs

That is half the value. Whatever nothing watches goes stale **silently**, and
you find out the day you rebuild, which is the worst possible moment.

---

## 8. Phase II

- **mTLS, and per caller.** With bearer, the secret crosses the wire and the
  destination receives it whole on every call; with mTLS the private key never
  leaves home and nothing reusable is left in transit, in a log or in a proxy.
  It still does not prove *which machine* is calling — it proves who holds the
  key — but a key that does not travel is far harder to steal.

  The part that matters for this design: **it is a per-caller dial, not a
  per-phone one.** The same number can require a bare token from one caller and
  a client certificate from another, because the `auth` column says so. That is
  how you raise the bar for a caller you trust less, without capping the agent
  for everyone — which is the whole point of putting policy at the door.

  Open question: where the fleet CA lives and who guards it.
- **Callback webhook**, once there are enough long errands to pay for the
  double registration per pair.
- **Talking to a live tmux session.** Still the missing piece and the most
  valuable one.
- **Socket activation**, which would take an idle phone down to nothing.

---

## 9. Still to build

Already built, so the rest can be read against it:

- **The launch answering with full permissions** — `--full-permissions`, so the
  agent can write, commit, deploy and `sudo`. Verified in both directions: with
  it, `sudo -n id -un` returns `root`; without it, the same call dies with
  *"This command requires approval"* and there is nobody to approve it.
- **A runaway bounded** — `--max-turns` and `--max-budget`. What was written
  here before, and is false: that `--allowed-tools` made a scope reach the
  launch. It does not bind. See the correction above.
- **Expiry enforced**, per caller, in `callers.expires_at`. Verified both ways
  on the phone-wide flag it replaced: a past date refused to start, and a
  25-second expiry took the same call from 200 to a refusal.

  The phone-wide version (`--token-expires`, with the shared token file it
  belonged to) is **gone in v0.3.0**. It expired a credential rather than an
  access, so it went on being enforced after the table had taken over — and
  since a past date refuses to start the service, a machine could be killed by
  a deadline belonging to a token nobody had presented in weeks. An expiry
  belongs to the caller it admits, next to the origins it admits them from.

Still missing:

- ~~The command line tool and the databases.~~ **Built**: `a2aagentphone-admin`
  against one `phone.db` per phone.
- ~~**Multiple tokens on the ear.**~~ **Built, and the single one removed
  entirely in v0.3.0.** A credential per caller, each with its own origins,
  expiry and revocation, so the log can say *who* called.

  Keeping the old shared token as a fallback for a while looked like
  kindness — an upgrade that stops accepting the credential everyone holds is
  not an upgrade — and it was a mistake worth recording, because the harm was
  not the extra code path. It was that **the fallback was conditioned on the
  table having an unrevoked caller**, so revoking the last one did not close
  the line: it stepped backwards onto a credential that was still sitting on
  disk. A safety net attached to the thing it is meant to catch.

  The general shape, which is worth carrying to the next deprecation: a
  fallback that triggers on *absence* inverts the meaning of removing the
  last item. If removal is supposed to mean "closed", nothing may treat empty
  as "not configured yet".
- ~~**The call log.**~~ **Built in v0.1.20.** The argument above promoted it
  from useful to necessary — when the agent is powerful on purpose, the record
  is the only control left — but the reason it got built first was narrower and
  more concrete: **making a retry safe.**

  Schema and rationale in `a2aagentphone/db.py`. The decision that makes it work:
  **a row is inserted when the call arrives, before the agent starts**, and
  updated when it ends. Write only on completion and a call killed halfway
  leaves no trace at all, which is exactly the call you need. So an old row
  with `finished_at IS NULL` is not a gap in the record — it *is* the record:
  this ran, nobody learned how it ended, go and look at what it left.

  Refused calls are logged too; they had been leaving no trace anywhere. Of the
  credential presented, eight hex of its hash and never the value — enough to
  tell one wrong token retrying from a sweep with five hundred different ones.

  Still missing, and it is the same gap as everywhere: with one shared token
  the log records an **address**, not a name. `callers` is what turns it into
  attribution.

  And there is a half of this that had not been counted, reported by
  scm-intranet on 2026-09-22 from the side nobody was looking at. Cost, turns
  and denials were handed to the **caller** and to nobody else; the journal on
  the machine that did the work held the prompt and nothing more. So **the
  machine could not say how its own call ended** — twice in one day an operator
  standing on it could not tell why a call had died there. The outcome is now
  logged locally on every call, which removes the blindness; the log that says
  *who rang* still needs the `callers` table.
- The daemon and its socket. Today the token sits in an environment variable the
  agent could read.
- The origin filter, which today has to sit in the reverse proxy — the wrong
  place, because adding a caller should never mean editing a proxy.
- **A second phone for the same agent.** Reception and operations, as above.
  Nothing blocks it; it just has not been set up.

And two things worth keeping in mind:

- **The `contextId` → Claude session map lives in the ear's memory.** Restart it
  and conversations in flight lose their thread.
- **If a machine changes address, `allowed_from` stops matching.** It belongs on
  the checklist for moving or renaming a machine, next to the map.
