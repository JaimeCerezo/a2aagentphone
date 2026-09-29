# Setting up a phone, on your own

## Read this part only

```bash
curl -fsSLO https://raw.githubusercontent.com/JaimeCerezo/a2aagentphone/main/scripts/install.sh
sudo bash install.sh --name <agent> --url https://<public-name>/ \
                     --user <who answers> --knows <the agent's project>
```

`--knows` is where the agent's real project lives — `/srv/projects/<name>` or
wherever your repository is. It goes into the starter `CLAUDE.md` so the
answering agent begins every call knowing **where its own knowledge is**,
rather than only knowing which machine it is standing on. Leave it out and the
file says so, in a line somebody has to come back and fill in.

That is the whole install: package, phone folder, token, unit file, firewall,
enable at boot, and four checks that it really answers. It is idempotent — run
it again to update, and it keeps your token and your `CLAUDE.md`.

To update later, cheaper still, no arguments at all:

```bash
curl -fsSLO https://raw.githubusercontent.com/JaimeCerezo/a2aagentphone/main/scripts/update.sh
sudo bash update.sh            # every phone on this machine
sudo bash update.sh --check    # look, change nothing
```

> **If you are ordering an update over the phone: hang up, then call back.**
> The agent you are talking to answers *through* the service it is about to
> restart. The updater defers the restart until the line is clear, so it lands
> the moment that agent hangs up — taking its reply with it. Measured on
> 2026-09-22: the update succeeded and the caller got `exit 143` and no answer.
> The work was done; only the acknowledgement was lost.
>
> And you rarely need to call back at all. **The agent card is unauthenticated
> and carries the version**, so checking costs nothing and starts no agent:
>
> ```bash
> curl -s https://<their phone>/.well-known/agent-card.json | grep -o '"version":"[^"]*"'
> ```
>
> Asking *"did you update?"* costs a real call and can die on the restart.
> Reading the card cannot.

**The two halves do not arrive by the same road**, and knowing that saves a
diagnosis. The script is fetched from `main`, so a fix in `scripts/update.sh`
lands on the very next run, tagged or not. The package is installed with
`pip install git+…@<tag>`, because `resolve_version` picks by
`git ls-remote --tags` on purpose — no CDN in front. So a commit on `main` that
raises `__version__` **does not exist for the fleet until somebody tags it**:
the updater will say `package already current`, and it is right. Measured by
scm-intranet on 2026-09-22 with 0.3.5 sitting untagged: the new guard was
already running, and only the version card still read 0.3.4.

**Three things the script cannot do for you**, and they are the only decisions
left:

1. **Choose the user that answers** (`--user`). A caller with a valid token
   makes an agent act as that user. Pick the working account when the token
   stays with someone who already has SSH here, or when the point is real work.
   Pick a user with nothing when the number goes to callers you would not hand
   a shell to. **Do not cripple a phone to make it safe — give a poorer user its
   own number.**
2. **Give it TLS** (`--url`). The token travels in a header; without TLS anyone
   on the path can start an agent on your machine. Point a route from whatever
   proxy you already run at the host and port the script reports. The script
   will tell you if the public URL is not answering yet.
3. **Admit the callers.** The script mints no credential — a fresh phone
   refuses everybody until a person runs `a2aagentphone-admin caller add`, one
   caller at a time, each with its own token and expiry (§4). Hand over the
   token's **path**, never its value: callers fetch it over SSH, redirected
   straight to a file, so it never enters a conversation.

**What is not yours to choose:** the turn and budget limits are fleet constants
set by the script, and the code is not modified locally. See
[GOVERNANCE.md](GOVERNANCE.md).

## Migrating an install that was set up by hand

The script owns `/opt/a2aagentphone/venv`, `/etc/a2aagentphone/<name>.env` and the
`a2aagentphone@<name>` unit. If your phone lives anywhere else, do this **in order**,
or you will end up with two phones fighting over the same port:

```bash
sudo cp /etc/a2aagentphone/<old>.env /etc/a2aagentphone/<new-name>.env  # keeps token + folder
sudo systemctl disable --now a2aagentphone@<old>
sudo rm /etc/a2aagentphone/<old>.env                                # so it cannot start
sudo bash install.sh --name <new-name> --url https://… --user <user> \
                     --host <your host> --port <your port>
```

Step 1 is what saves the token: the script reads the existing `.env` and keeps
the credential and the project folder rather than minting new ones. Pass
`--host` and `--port` explicitly — the autodetect picks Docker's *default*
bridge, which is often not the one your proxy is on.

> **Before deleting the old venv, repoint anything that referenced it.** An
> outgoing MCP contact pointing at `…/oldvenv/bin/a2aagentphone-mcp` does not fail
> when you restart. It fails on the next call, and from the far end it looks
> like the other agent has stopped answering. Use
> `/usr/local/bin/a2aagentphone-mcp`, which the script keeps current. It warns you
> if it finds such a reference, but check anyway — nothing else will.

**Stop here if it worked.** Everything below is the reasoning, the by-hand
version, and the traps — read it when something breaks or when you are deciding
something the script does not decide for you.

---

## 0. Three decisions, before any command

### Who answers the phone

The listener runs `claude` as **its own user** — a process cannot change user.
So whoever that user is, **a caller with a valid token can make an agent act
with that user's privileges.**

Two honest options, and the right one depends on **who will hold the token**:

| | Pick this when |
|---|---|
| **The working agent account, privileges and all** | The token stays with someone who already has SSH to this machine, or the point is for the caller to get real work done: installs, deploys, maintenance |
| **A dedicated user with the bare minimum** — no sudo, no docker group, no keys | The number goes to callers you would not hand a shell to |

If you choose the first, know what you are choosing: **a token against that
number is permission to command an operator who is root on your machine.** For
someone who already holds SSH with sudo, that adds no risk — it is another door
to a building they have keys to. For anyone else, it is the whole building.

The honest asymmetry: an SSH key is used by a person, and a person cannot be
talked into using it by something they read. An agent can. That is the one real
difference, and it is about the holder, not about the phone.

A dedicated user needs its own Claude authentication, which needs a real
terminal. Plan for a human to run that step.

### What the caller may do — and the correction that cost us a day

**Use `--full-permissions`.** Without it the phone answers with a mutilated
agent: it can read, it can run read-only shell, and every single thing you
opened the phone up to *do* — write a file, commit, deploy, `sudo` — dies
unapproved, because there is no terminal and therefore nobody to approve it.

An earlier version of this guide said `--allowed-tools` "is the only limit that
actually binds". **That was wrong**, and it was wrong in the dangerous
direction. scm-intranet reproduced the exact launch on 2026-09-22 and measured
it:

- **`--allowedTools` adds to what is auto-approved. It subtracts nothing.** The
  model's catalogue is not trimmed: launched with `"Read,Glob,Grep"` it still
  sees `Bash`, `Write`, `Edit`, `Agent`, `WebFetch`.
- **An agent cannot tell from its own tool list whether it has been
  restricted** — which means it cannot honestly report its own limits.
- What actually stopped anything was the permission layer: `Write` died for
  want of a TTY, **but `Bash` ran**. So a phone advertised as read-only was
  never read-only — it could run arbitrary read shell. Same reach as `Read`,
  so no new exposure; but the unit file and the phone's own `CLAUDE.md` were
  telling a lie.

If you genuinely want a tool barred, the thing that binds is
**`permissions.deny`**. And mind the asymmetry, also measured: in a workspace
that is not trusted, `permissions.allow` is ignored in silence — only a warning
on stderr — while `deny` is still honoured.

But before reaching for `deny`, reread [DESIGN.md](DESIGN.md), *"The principle:
all the policy is at the door"*. Crippling the phone is a blunt control: it
hits your trusted callers exactly as hard as the hostile one, and it protects
you from nobody who already holds SSH. **If you want a restricted phone, do not
restrict this one. Give a less privileged user its own number.**

### What one call may cost

Use **`--max-budget`**, in dollars. Prefer it to `--max-turns`: a turn cap is
only a proxy — one turn can be expensive and a cheap question can need six —
while money is what is actually being spent.

Keep a turn cap too, but loose, as a backstop against a loop rather than as the
real limit.

Know what the budget does and does not do: it is checked **between turns**, so
the first turn runs to completion whatever it costs. Measured: a 0.01 cap still
spent 0.064. It bounds a runaway, not a single expensive answer.

**Size it to the work, not to the question.** This is where we got it wrong, so
take the numbers rather than the reasoning:

| What the phone is for | Measured | Sensible cap |
|---|---|---|
| Answering questions about a project | 0.05–0.40 | **2.00** |
| Doing the work — edit, commit, push, deploy | 1.9–3.6 | **5.00** |

Both caps are well above the measured cost, and that is the point. **A cap set
near the typical cost is not a safety limit, it is a coin toss**: the question
that needs one more look, or the file that is longer than the last one, hits it
and pays in full for nothing. The cap exists to stop a loop, not to haggle over
a normal answer — so leave several times the usual cost of headroom and let it
be boring.

A cap sized for answering, on a phone that can act, is the worst of both: it
does not bound anything useful, and it kills real work halfway. Measured on
2026-09-22 — a 0.40 cap on a production task spent the whole 0.40, returned
nothing, and still left changes behind.

> **The failure mode, and it is the one that should worry you.** The cap is
> checked between turns and the turn in flight is never interrupted, so the
> agent is killed **wherever it stands**. On that same call it had committed
> but not pushed, and had already deployed — so the live site and the
> repository disagreed for minutes, **with no error raised anywhere**. While a
> phone only answers, an exhausted budget is a wasted dollar. Once it can act,
> it is a silent inconsistency.
>
> a2aagentphone tells the agent its budget at the start of every call and asks it to
> work in a cut-survivable order — push before deploying, small complete steps,
> stop and report rather than be killed mid-write. That is mitigation, not a
> guarantee: **after a call dies on budget, go and look at what it left.**

### How it gets TLS

**Not optional.** The token travels in a header. Without TLS anyone on the path
reads it, and with it they can start an agent on your machine.

Work out what you already have (step 1). If you have no reverse proxy at all,
stop and say so — this tool cannot terminate TLS by itself, and that is a real
gap, not something to work around with plain HTTP.

---

## 1. Survey your own machine first

Do not assume. Run these and read the answers:

```bash
# Is there a reverse proxy, and which?
ss -ltnp 2>/dev/null | grep -E ':(80|443)\b'
docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null
command -v caddy nginx apache2 2>/dev/null

# Python: the package needs >= 3.10
python3 -V
python3 -c "import venv" 2>&1 | tail -1   # some distros ship venv separately

# The claude this machine will actually run, as the user that will answer
command -v claude && claude --version

# Are you inside a container?
[ -f /.dockerenv ] && echo "inside a container" || echo "on the host"

# Firewall, which will bite later
sudo ufw status 2>/dev/null | head -5
```

Two things to notice:

**If `python3 -c "import venv"` fails**, install the distro package
(`python3-venv` or `python3.12-venv`) before anything else.

**If you are inside a container**, the networking below changes: the listener
binds inside the container and the proxy reaches it by container name on a
shared network, not through a host bridge address.

---

## 2. Install

```bash
python3 -m venv ~/a2aagentphone-venv
~/a2aagentphone-venv/bin/pip install "git+https://github.com/JaimeCerezo/a2aagentphone@v0.1.12"
~/a2aagentphone-venv/bin/a2aagentphone --help
```

**Pin the tag.** Not `main`. A commit is a hash of its content, so a pinned
version is a promise nobody can break — and when something misbehaves later,
the first useful question is "which version is that box running?"

---

## 3. Give the phone a project folder

The answering agent reads its `CLAUDE.md` like any other Claude Code session.
That file is what makes the answer *your agent's* answer rather than a generic
model's.

```bash
mkdir -p ~/a2aagentphone-phone
```

Write a `CLAUDE.md` in it covering:

- **Who it is** and what it is allowed to talk about.
- **How to answer**: briefly. The caller is paying and is holding an HTTP
  connection open.
- **What to refuse**: anything outside its purpose. Be explicit — this is
  judgement, not a barrier, but judgement written down works better than
  judgement improvised.
- **A control word you invent.** A caller asks for it to confirm that your
  agent answered and read its own knowledge. Make it distinctive.
- **Where its knowledge lives** — the `--knows` line. Without it the agent
  starts every call knowing who it is and nothing about what it looks after.

### This folder must NOT be the agent's project

It looks like a mistake worth fixing. It is not, and the reason only bites
weeks later.

The answering agent runs `claude` in this folder, so its calls land in the
transcript history of this `(user, folder)` pair. A machine's live session
usually resumes with **`claude --continue`**, which means *"the most recent
transcript for that pair"*. Point the phone at the project and every call drops
a transcript into the same pair — so the next restart of the live session
resumes **a phone call** instead of the conversation somebody was having.

The phone never steals the thread the other way: it uses `--session-id` and
`--resume` with explicit identifiers, never `--continue`. The risk runs in one
direction only, and it does not show up on install day.

So keep the folder separate and point the agent at its project with `--knows`.

What that separation does **not** buy you is confinement. `--cwd` sets where
the agent starts, not what it can reach: measured, an agent with only `Read`
and `Glob` read `/etc` and another machine's unit files without leaving its
allowed tools. If you want a poorer phone, the lever is the user, not the
folder.

### One phone, one posture

If you want a restricted phone, **do not restrict this one — add another.** A
second user with fewer privileges, its own port, its own number. A phone is
self-contained precisely so that this costs one more unit file.

That is cleaner than capping a single phone, for a reason worth keeping in
mind: **the user is the restriction.** Tools and deny rules are settings that
can be edited, forgotten, or widened by whoever next touches the config. A user
without sudo, without docker and without keys cannot be talked into having them.

So the question is never "how much do I cap this phone", it is "how many phones
do I need, and who answers each".

### Deny rules: available, and not always wanted

If you do want a hard limit, `.claude/settings.json` in the project folder is a
real one — the tool call is refused by configuration, not by the agent's
judgement, and it never sees the content.

**But think about who holds the token before reaching for this.** If the caller
already has SSH with sudo to this machine, restricting the agent protects
nothing: they have another door, and all you have done is make the phone worse
at its job. Capping the agent is a blunt control — it applies to every caller
equally, including the ones you trust completely.

**The per-caller control is the token**, not the agent. Reach for deny rules
when the number is handed to callers you would not hand a shell to.

```json
{
  "permissions": {
    "deny": [
      "Read(//home/<user>/.ssh/**)",
      "Read(//home/<user>/.claude/**)",
      "Read(//etc/a2aagentphone/**)",
      "Read(//**/.env)",
      "Read(//**/*.cred)"
    ]
  }
}
```

Verified, not assumed: with the rule in place the agent reports *"File is in a
directory that is denied by your permission settings"* and cannot read it —
including files it wants to read and has no reason to refuse.

Note `--cwd` does **not** do this. It sets where the agent starts, not what it
can reach: with only `Read` and `Glob`, an agent read `/etc` and another
machine's unit files without leaving its allowed tools.

One caveat: the project's own `CLAUDE.md` is loaded into context at start, so
denying `Read` on it does not hide its contents. Deny rules bound what the
agent can *fetch*, not what it was already given.

---

## 4. Admit a caller

**Installing mints nothing.** A fresh phone has both lists empty: nobody can
call it and it can call nobody. That is the honest state of a phone that has
not been introduced to anyone, and it fails closed — every call gets a 401
until somebody is admitted.

Credentials appear one at a time, when a person decides to let someone in:

```bash
sudo a2aagentphone-admin --db /var/lib/a2aagentphone/<name> caller add <who> \
     --days 365 \
     --note "who this is and when you gave it to them"
```

**There is no `--from` any more.** It was required until v0.5.0, holding the
CIDRs a credential could be used from. Calls are no longer filtered by origin
at all: between a container's SNAT and a reverse proxy that owns
`X-Forwarded-For`, the address the listener could read was not reliably the
caller's, so the check was removed rather than left looking like it worked.
DESIGN.md §6 is the account.

What carries the weight instead: **`--days`**, and **which user answers this
phone**. The token is now the whole credential.

The token is printed **once**; only its hash is stored. Rules that matter more
than they look:

- **Never write it into a file that git tracks.** Deleting the commit does not
  help; the history is forever and the only fix is a new token.
- **Never repeat it in your own answers.** Anything you write stays in your
  transcript, unprotected and without an expiry. Write it to a `0600` file and
  give the other end the **path** — they fetch it over SSH, redirected
  straight into place, and it never passes through a conversation.
- **Give it an expiry** with `--days`. It is enforced on every call, which is
  what makes a short-lived credential safe to hand over at all.

> **Who runs this.** Adding a *new* caller is a person's job: an agent can be
> talked into opening the door by something it reads, and a permission prompt
> would be asked of an empty room. **Rotating** the credential of a caller
> that is already documented may be done by an agent — the door is not being
> opened, only its lock changed for somebody already listed.

### There is no shared token, and that is deliberate

Until v0.3.0 the installer minted one, and it was three things at once. Each
was a liability, and the third is the one that decided it:

- **It was the switch that mounted authentication.** A phone started without
  the file did not lose a fallback — it answered *everyone*.
- **It carried a phone-wide expiry** that refused to start the whole service,
  enforced long after the credential had stopped being used by anybody.
- **The fallback to it was conditioned on there being an unrevoked caller**,
  so revoking the last caller did not close the door: it silently reopened the
  shared token, still on disk with its original value.

If you are updating a machine that had one, `update.sh` strips both settings
from its `.env` and **deletes the token file**. It is already inert by then —
the listener stopped accepting it the moment the new version was installed.

---

## 5. Run it as a service

Pick a port nothing else uses (9110 is the convention).

**Where to bind** — this is the step people get wrong:

| Situation | Bind to |
|---|---|
| Proxy in a container, listener on the host | The docker bridge gateway (`docker network inspect <net> --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`) |
| Proxy and listener both on the host | `127.0.0.1` |
| Both inside containers on a shared network | `0.0.0.0` inside the container |

**Never bind `0.0.0.0` on the host.** That publishes the port to the internet
underneath your proxy, and the proxy is where your TLS and your limits are.

A systemd template unit, so more phones cost one file each:

```ini
# /etc/systemd/system/a2aagentphone@.service
[Unit]
Description=a2aagentphone phone (%i)
# docker.service matters when you bind to a docker bridge address: that
# address does not exist until docker is up, so without this the service
# restart-loops after a reboot.
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=exec
User=<the user that answers>
EnvironmentFile=/etc/a2aagentphone/%i.env
ExecStart=/home/<user>/a2aagentphone-venv/bin/a2aagentphone \
    --cwd ${A2A_CWD} \
    --name ${A2A_NAME} \
    --host ${A2A_HOST} \
    --port ${A2A_PORT} \
    --public-url ${A2A_PUBLIC_URL} \
    --db ${A2A_DB} \
    --full-permissions \
    --max-budget ${A2A_MAX_BUDGET} \
    --max-turns ${A2A_MAX_TURNS}
# always, NOT on-failure. The listener ends with `return 0`, so a clean
# shutdown exits zero and `on-failure` would leave the phone dead with
# nothing in any log — from systemd's side it finished correctly. `always`
# costs no control: an explicit `systemctl stop` is still respected.
Restart=always
RestartSec=5
# NoNewPrivileges=true would block privilege escalation -- including this
# agent's own sudo, which is half of what it is for. Left out on purpose. If
# you put it back, know that --full-permissions will no longer buy you sudo,
# and the phone will fail at exactly the jobs you opened it for. The way to get
# a phone that cannot escalate is a user that cannot escalate, on its own port.

[Install]
WantedBy=multi-user.target
```

**Do not `systemctl enable` it for a first test.** If the machine reboots, a
phone you are still evaluating should not come back by itself.

**And remember to enable it once it is real.** Easy to forget, because nothing
complains: the phone keeps working for days and simply never comes back after a
reboot. Found on a live install on 2026-09-22, where `After=docker.service` had
been added carefully to a unit that was still `disabled` — the ordering could
never matter, because the service was not being started at all.

`--public-url` must be the **HTTPS address callers will use**, not where it
binds. The card advertises this, and a card advertising `127.0.0.1` is a number
nobody can dial.

If the firewall is on and the proxy is in a container, you will need to let the
bridge reach the port:

```bash
sudo ufw allow from <bridge-subnet> to any port <port> proto tcp
```

---

## 6. Put TLS in front

### You need a name, and you do not need to own one

Let's Encrypt will not issue for a bare IP by the ordinary path. You do not
have to create a DNS record either: **`<your-ip>.nip.io` already resolves to
that address.** So `203.0.113.7.nip.io` works today, with a real certificate.

For production, prefer a name you control — depending on someone else's DNS for
your machines to talk is one more thing that can fail — but it does not block a
first install.

### Traefik

Traefik's Docker provider only routes to **containers**, by labels. A phone on
the host is not a container, so you need the **file provider**, which most
setups do not have yet. Add to its command:

```yaml
- --providers.file.directory=/dynamic
- --providers.file.watch=true
```

and mount a directory at `/dynamic`. That change needs a Traefik restart —
brief downtime for everything it serves, so pick your moment. After that,
adding or changing a route is hot-reloaded and costs nothing.

Then a file in that directory:

```yaml
http:
  routers:
    phone:
      rule: "Host(`<your-ip>.nip.io`)"
      entryPoints: [websecure]
      service: phone
      middlewares: [phone-ratelimit]
      tls:
        certResolver: <your resolver name>

  services:
    phone:
      loadBalancer:
        servers:
          - url: "http://<bind-address>:<port>"

  middlewares:
    # Every call starts an agent and costs money. This does not protect against
    # a stolen token; it protects against a stupid loop on the other end.
    phone-ratelimit:
      rateLimit:
        average: 6
        burst: 3
        period: 1m

    # There was a `phone-origin` ipAllowList here until v0.5.0, described as
    # temporary until the listener could do it. The listener now deliberately
    # does NOT do it -- see DESIGN.md section 6 -- so this one is gone too
    # rather than promoted.
    #
    # If you add one back, know what it costs: a caller inside a container on
    # this same host arrives SNAT-ed to the bridge gateway, so the narrowest
    # honest rule is the whole Docker network. That was measured, and the two
    # lists disagreeing is what produced a 403 here and a 401 there for the
    # same call. Your proxy is yours; just do not expect it to tell callers
    # apart.
```

**Do not enable the route before the name resolves.** Let's Encrypt will fail,
and failures burn your hourly quota, so getting the order wrong is not free.

### Caddy

```
<your-ip>.nip.io {
    reverse_proxy <bind-address>:<port>
}
```

### nginx

You will need `certbot --nginx` for the certificate, `proxy_pass` to the bind
address, and `limit_req` for the rate limit.

### Nothing at all

Say so and stop. Plain HTTP means handing the token to anyone on the path.

---

## 7. Check it yourself before telling anyone it works

All four, in order. Each one catches a different mistake.

```bash
# 1. The card is served, unauthenticated by design, and advertises the
#    PUBLIC address -- not the bind address.
curl -s https://<name>/.well-known/agent-card.json

# 2. No token -> 401. This also passes on a phone nobody is admitted to yet,
#    which is correct: an empty callers table refuses everybody.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<name>/ \
  -H 'A2A-Version: 1.0' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"role":"ROLE_USER","parts":[{"text":"x"}],"messageId":"1"}}}'

# 3. With the token -> the control word from your CLAUDE.md.
curl -s -X POST https://<name>/ \
  -H "Authorization: Bearer $(cat ~/.a2aagentphone-token)" \
  -H 'A2A-Version: 1.0' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"role":"ROLE_USER","parts":[{"text":"Who are you and what is your control word?"}],"messageId":"2"}}}'

# 4. The port is NOT reachable from outside, bypassing the proxy.
#    From another machine, or ask whoever is coordinating:
#    nc -z -w5 <your-public-ip> <port>   ->  must refuse
```

Check 3 is the real one. If it answers but **without your control word**, the
agent is not reading its `CLAUDE.md` — usually the wrong `--cwd`.

---

## 8. Report back exactly this

Whoever is going to call you needs all of it, and they cannot look:

- The **HTTPS URL**.
- The **control word**, so they can confirm your agent answered.
- **Which user answers the phone**, and what it can reach. Say it plainly —
  this is what the caller is being trusted with.
- The **tools** it runs with, and the turn cap.
- **When the token expires**, as an exact instant with its timezone.
- **Which IP you allowed**, so a 403 is diagnosable instead of mysterious.
- The **version** you installed.

Send the **token separately**, not in the same message.

---

## Traps that will cost you an hour

**The method is `SendMessage`, not `message/send`.** The latter is the 0.3
compatibility layer. And you must send **`A2A-Version: 1.0`** or the server
assumes 0.3 and rejects the call with an error that does not say why.

**`--public-url` is not where it binds.** Get this wrong and everything works
locally while every caller gets a card pointing at an address they cannot
reach.

**The proxy's own timeouts.** Four clocks are involved — the caller, the proxy,
the listener, and `claude` itself. They should grow outwards, innermost
shortest, so whoever gives up first is the one who knows why. The proxy's
defaults are the ones everybody forgets, and if the proxy is the shortest it
cuts the call and nobody else notices.

**Do not put an origin check back.** Not in the listener — it was there, and
removed in v0.5.0 — and think twice in the proxy. The trap that makes it
attractive is that it *looks* like it works: the real address is the **last**
entry of `X-Forwarded-For`, the earlier ones can be written by the caller, and
the classic bug is taking the first. But even done right it cannot tell apart
the callers you actually have, because a container calling its own host arrives
as the bridge gateway. DESIGN.md §6.

**A restart does not extend the expiry.** The deadline is fixed at start from
the environment file. To extend it you edit it, deliberately.

---

## If you get stuck

Report the **exact error** rather than routing around it. A clean failure says
more than a workaround that succeeds by another path — and whoever is
coordinating cannot see your machine, so a paraphrase costs a round trip.

Useful:

```bash
systemctl status a2aagentphone@<name> --no-pager -n 30
journalctl -u a2aagentphone@<name> -n 50 --no-pager
```

| Symptom | Usually |
|---|---|
| `403` | The caller's address is not the one you allowed |
| `401 unauthorized` | Any of the four caller checks: unknown token, revoked, expired, or calling from an address outside its `--from`. **They are deliberately indistinguishable** — check with `a2aagentphone-admin caller list`, not by guessing |
| `401` on a brand new phone | Nobody admitted yet. `caller add` |
| Card shows `127.0.0.1` | `--public-url` not set |
| Certificate fails | The name did not resolve when the route went live |
| Proxy cannot connect | Bound to `127.0.0.1` while the proxy is in a container, or the firewall |
| Answers, no control word | Wrong `--cwd`, so no `CLAUDE.md` |
| Restart loop after a reboot | Bound to a docker bridge address without `After=docker.service` |
