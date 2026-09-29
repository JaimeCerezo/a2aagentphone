"""The deployment, as data: fleet constants and the unit file, in one place.

These used to live in ``install.sh``, and that is the bug this module exists to
kill. It surfaced three times in one day, each time wearing a different hat:

* ``update.sh`` upgraded the package but never re-rendered the unit, so a phone
  that had been *updated* kept whatever ``ExecStart`` it was installed with.
  On 2026-09-22 that meant a current phone running without ``--db`` — it
  answered calls perfectly and recorded not one of them. Nobody could tell,
  because a missing log looks exactly like a quiet week.
* the command-line tools were placed by the installer and not by the updater,
  so an updated machine could be on the newest version with none of the
  commands that make it usable.
* and the limits are supposed to be fleet constants, but only the installer
  knew them, so an old phone kept old limits forever.

The pattern underneath all three: **a machine that updates is not the same as a
machine that reinstalls**, and anything only the installer knows drifts away
silently on every machine that takes the cheap path — which is every machine,
because the cheap path is the one we told them to take.

So the deployment is defined here, inside the package. Both scripts render it
from whatever version is installed, and neither holds a second copy that can
fall behind.
"""

from __future__ import annotations

import sys

# --------------------------------------------------------------------------
# Fleet constants. Deliberately not options.
#
# Every phone runs the same limits, so that "will this call fit?" has one
# answer everywhere instead of one per machine. A limit that varies by host is
# a limit nobody can reason about from the calling side.
#
# Measured: a question costs 0.05-0.40, real work costs 1.9-3.6. The budget is
# the real limit; the turn cap is only a backstop against a loop, so it sits
# well above where money runs out. It was 20 for a while, which quietly made
# turns the binding limit and undid the whole argument.
# --------------------------------------------------------------------------
MAX_TURNS = 60
MAX_BUDGET = "5.00"

PREFIX = "/opt/a2aagentphone"
VENV = f"{PREFIX}/venv"
ETC = "/etc/a2aagentphone"
STATE = "/var/lib/a2aagentphone"

# Every command the phone ships. All of them are console scripts of this
# package, so `pip install` puts them in the venv and converge() links them
# where people can reach them. Nothing is fetched separately and nothing can be
# placed by one script and forgotten by the other -- which is how the tools,
# the limits and the unit each went stale in turn on 2026-09-22.
COMMANDS = ("a2aagentphone", "a2aagentphone-mcp", "a2aagentphone-admin", "a2aagentphone-note", "a2aagentphone-log")
BINDIR = "/usr/local/bin"
MAILBOX = f"{STATE}/mailbox.md"

# Settings that no longer mean anything, removed from every .env on update.
#
# The single shared token and its phone-wide expiry, both gone in v0.3.0.
# Leaving them would not be harmless: a machine whose .env still names a token
# file is a machine where somebody eventually restores the file and wonders why
# it does nothing -- or worse, where the expiry date is read as if it still
# governed anything. An update is exactly where a retired setting should
# disappear, for the same reason the limits are rewritten rather than merged.
RETIRED = ("A2A_TOKEN_FILE", "A2A_TOKEN_EXPIRES")

UNIT = """\
[Unit]
Description=a2aagentphone phone (%i)
# docker.service because a phone often binds to the bridge and is fronted by a
# proxy in a container. Without this it restart-loops after a reboot.
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=exec
User={user}
# Without this the phone says nothing at all. Python block-buffers stdout when
# it is not a terminal, and a service never exits, so the buffer never
# flushes: the whole startup banner -- version, where it listens, what it
# advertises, how many callers, permissions, budget -- was invisible in the
# journal on every machine, always. Measured on 2026-09-22: zero banner lines
# across hours and several restarts.
#
# The line that made it worth a release is not the banner, though. It is this
# one, printed at startup and also to stdout:
#
#     NOTE: n call(s) started and never finished
#
# That is the warning meant to be read after a call died mid-write, and nobody
# had ever seen it.
Environment=PYTHONUNBUFFERED=1
EnvironmentFile={etc}/%i.env
# Optional, and YOURS: this file is never written, merged or removed by the
# installer, unlike the one above. It exists for one setting.
#
# The agent that answers needs a Claude credential, and there are two shapes.
# A stored login in the answering user's $HOME works and is what most phones
# use. But it carries a refresh token, and a refresh token rotates when it is
# used -- so the moment a machine has several system identities for the same
# agent (`-agent`, `-cron`, `-phone`) sharing a copy of that file, they
# invalidate each other, silently, and the phone stops being able to answer
# without anything saying so.
#
# A setup token does not refresh and cannot collide, but it arrives as an
# environment variable, and a unit does not inherit one from anywhere. Hence
# this line. Put CLAUDE_CODE_OAUTH_TOKEN in that file, mode 0640, or point it
# at wherever the token already lives -- a symlink is read fine, and one file
# with two names beats two files with one secret.
#
# The leading `-` means a phone that does not need it starts exactly as before.
EnvironmentFile=-{etc}/%i.credentials.env
ExecStart={venv}/bin/a2aagentphone \\
    --cwd ${{A2A_CWD}} \\
    --name ${{A2A_NAME}} \\
    --host ${{A2A_HOST}} \\
    --port ${{A2A_PORT}} \\
    --public-url ${{A2A_PUBLIC_URL}} \\
    --db ${{A2A_DB}} \\
    --full-permissions \\
    --max-turns ${{A2A_MAX_TURNS}} \\
    --max-budget ${{A2A_MAX_BUDGET}}
# always, not on-failure. The listener ends with `return 0` after uvicorn, so
# any clean shutdown that is not an exception exits ZERO -- and `on-failure`
# does not consider zero a failure. The phone would stay dead with nothing in
# any log to say why, because from systemd's side it finished correctly.
#
# `always` costs no control: systemd does not restart after an explicit
# `systemctl stop`, whichever policy is set. What it adds is the case nobody
# watches -- the quiet exit at 3am on a machine where the only sign is that
# calls stop being answered.
Restart=always
RestartSec=5
# NoNewPrivileges is deliberately absent: it would block the agent's own sudo,
# which is half of what the phone is for. A phone that must not escalate is one
# answering as a user that cannot escalate -- not this flag.

[Install]
WantedBy=multi-user.target
"""


def unit_text(user: str) -> str:
    return UNIT.format(user=user, etc=ETC, venv=VENV)


def _retire_token_file(path: str, changed: list[str]) -> None:
    """Destroy the retired shared token, rather than leaving it on disk.

    Deliberately a delete and not a rename, which is the opposite of what
    :func:`a2aagentphone.db._absorb` does with a superseded database -- and the
    difference is the point. A migrated database is *evidence*, worth keeping
    so the migration can be checked afterwards. A retired credential is a
    *liability*: its whole value to an attacker survives being renamed, and
    nobody ever needs to read it again.

    It is already inert by the time this runs -- the ear stopped accepting it
    the moment this version was installed -- so this removes the object, not
    the access.
    """
    import os

    if not path or not os.path.isfile(path):
        return
    try:
        os.remove(path)
        changed.append(f"retired shared token {path} DELETED")
    except OSError:
        # Said rather than swallowed: a token that could not be deleted is
        # exactly the one somebody has to go and delete by hand.
        changed.append(f"COULD NOT DELETE the retired token at {path} -- remove it yourself")


def converge(user: str | None = None) -> list[str]:
    """Put the machine into the state an a2aagentphone install is supposed to be in.

    **One code path, called by both scripts**, and that is the whole reason it
    exists. Installing and updating used to place different things, so every
    machine that took the cheap path -- which is every machine, because the
    cheap path is the one we tell them to take -- quietly lacked whatever had
    been added since it was first set up. It happened three times in one day
    with three different symptoms: missing commands, stale limits, and a phone
    running without --db that answered perfectly and recorded nothing.

    Safe to run as often as you like. Returns what it changed, so the caller
    can decide whether a restart is warranted.
    """
    import os
    import pwd

    changed: list[str] = []

    # The commands, at stable paths. A contact list and a crontab point at
    # these, never into the venv: an install that moves or is rebuilt would
    # otherwise break them -- and not at startup, but on the next call, which
    # looks exactly like the other end not answering.
    os.makedirs(BINDIR, exist_ok=True)
    for name in COMMANDS:
        src, dst = f"{VENV}/bin/{name}", f"{BINDIR}/{name}"
        if not os.path.exists(src):
            continue
        if os.path.islink(dst) and os.readlink(dst) == src:
            continue
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(src, dst)
        changed.append(f"command {name} linked")

    # The mailbox, before the phone answers its first call: "do not modify the
    # tool" is only fair if there is somewhere for a finding to go. An agent
    # with nowhere to report something reports it by patching.
    os.makedirs(STATE, exist_ok=True)
    if not os.path.exists(MAILBOX):
        with open(MAILBOX, "w", encoding="utf-8") as fh:
            fh.write("# a2aagentphone — mailbox\n\nNotes from agents on this "
                     "machine. Append only.\n")
        os.chmod(MAILBOX, 0o666)
        changed.append("mailbox created")

    # The unit, rendered from this version rather than from a copy in a shell
    # script that can fall behind.
    unit = "/etc/systemd/system/a2aagentphone@.service"
    if user is None and os.path.exists(unit):
        with open(unit, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("User="):
                    user = line.split("=", 1)[1].strip()
                    break
    if user:
        wanted = unit_text(user)
        current = ""
        if os.path.exists(unit):
            with open(unit, encoding="utf-8") as fh:
                current = fh.read()
        if current != wanted:
            with open(unit, "w", encoding="utf-8") as fh:
                fh.write(wanted)
            changed.append("unit file rewritten")

    # Every phone's settings and databases. The fleet constants are rewritten
    # rather than merged: a limit that drifted is a limit that has to come back
    # into line, and the rest of the file is the machine's own business.
    if os.path.isdir(ETC):
        for entry in sorted(os.listdir(ETC)):
            if not entry.endswith(".env"):
                continue
            name = entry[:-4]
            path = f"{ETC}/{entry}"
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            wanted_pairs = {
                "A2A_DB": f"{STATE}/{name}",
                "A2A_MAX_TURNS": str(MAX_TURNS),
                "A2A_MAX_BUDGET": MAX_BUDGET,
            }
            out, seen, dirty = [], set(), False
            for line in lines:
                key = line.split("=", 1)[0]
                if key in RETIRED:
                    # The single shared token, gone in v0.3.0. Its settings are
                    # stripped rather than left lying around: a dead credential
                    # that is still named in a config file is one somebody will
                    # eventually try to use, or restore.
                    dirty = True
                    if key == "A2A_TOKEN_FILE":
                        _retire_token_file(line.split("=", 1)[-1].strip(), changed)
                    continue
                if key in wanted_pairs:
                    seen.add(key)
                    new = f"{key}={wanted_pairs[key]}"
                    dirty |= new != line
                    out.append(new)
                else:
                    out.append(line)
            for key, value in wanted_pairs.items():
                if key not in seen:
                    out.append(f"{key}={value}")
                    dirty = True
            if dirty:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(out) + "\n")
                changed.append(f"{name} settings updated")

            # The databases, all of them, even the ones nothing writes to yet.
            # An empty table costs nothing; a missing one turns the day you
            # need it into a migration on a live phone.
            from . import db as db_mod

            directory = f"{STATE}/{name}"
            fresh = not os.path.isdir(directory)
            db_mod.init(directory)
            owner = user
            if owner:
                try:
                    info = pwd.getpwnam(owner)
                    os.chown(directory, info.pw_uid, info.pw_gid)
                    # Everything in there, not a list of names. The list was
                    # "callers.db, contacts.db" and the day the schema became
                    # one phone.db the file was created by root and the
                    # service could not open its own database. A hardcoded
                    # list of filenames is a rule that stops being true
                    # silently.
                    for f in os.listdir(directory):
                        try:
                            os.chown(f"{directory}/{f}", info.pw_uid, info.pw_gid)
                        except OSError:
                            pass
                except (KeyError, PermissionError):
                    pass
            # Not a flat 0700: a machine whose operator opened this directory to
            # a group is telling us several identities are the same agent behind
            # one number, and an install must not undo that. See
            # db._group_shared for what it cost to learn.
            db_mod._chmod_if_needed(directory, db_mod._dir_mode(directory))
            if fresh:
                changed.append(f"{name} databases created")

    return changed


def main(argv: list[str] | None = None) -> int:
    """Tiny CLI for the shell scripts. Not meant for people."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "unit":
        if len(args) < 2:
            print("usage: -m a2aagentphone.deploy unit <user>", file=sys.stderr)
            return 2
        sys.stdout.write(unit_text(args[1]))
        return 0
    if args and args[0] == "converge":
        user = args[1] if len(args) > 1 else None
        # Each item carries its own verb. A single trailing "updated" turned
        # "retired shared token /etc/a2aagentphone/x.token" into a line claiming the
        # file had been updated, when converge had just deleted it -- the one
        # destructive thing it does, described as the mildest.
        for item in converge(user):
            print(f"  {item}")
        return 0
    if args and args[0] == "constants":
        # What the shell scripts need from the package, as assignments they can
        # `eval`. Only what a script actually uses: a fourth line printed a
        # TOOLS list that nothing has read since converge() took over placing
        # the commands, and the variable behind it had been renamed to
        # COMMANDS -- so this crashed with NameError on every single install.
        #
        # It survived because the three lines above are already on stdout by
        # then, and `eval "$(cmd)"` reports the status of the eval, not of the
        # substitution, so `set -e` never saw it. Cost while it lasted: a
        # traceback in the middle of every install, which teaches whoever runs
        # it to ignore tracebacks. Cost if it had lasted: any constant added
        # below this point would never have reached the installer, in silence
        # -- which is the exact failure this module exists to kill.
        #
        # Every value is quoted, and that is a bug of its own worth not
        # repeating: unquoted, `eval` read `TOOLS=a2aagentphone-note a2aagentphone-log`
        # the only way it could -- assign for one command, then RUN the second
        # word. Every update quietly printed the call log. Harmless that time.
        print(f'MAX_TURNS="{MAX_TURNS}"')
        print(f'MAX_BUDGET="{MAX_BUDGET}"')
        print(f'STATE="{STATE}"')
        return 0
    print("usage: -m a2aagentphone.deploy {unit <user>|constants|converge [user]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
