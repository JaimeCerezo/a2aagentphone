"""``a2aagentphone-note`` — leave a note for whoever maintains this tool.

The mailbox exists so that "do not modify the phone" is not the same as "keep
it to yourself". You found something; write it here and carry on. Nothing waits
on a reply and you do not need permission.

    a2aagentphone-note "the budget cap killed a call mid-write; it had committed
                   but not pushed, and the deploy had already gone out"

    some-command 2>&1 | a2aagentphone-note "unit fails to start after reboot"

In the package rather than shipped as a loose script, and that is the point:
anything the installer placed and the updater did not went stale on every
machine that took the cheap path. Now it arrives with the wheel.
"""

from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

MAILBOX = Path(os.environ.get("A2AAGENTPHONE_MAILBOX", "/var/lib/a2aagentphone/mailbox.md"))

USAGE = """\
usage: a2aagentphone-note "what you saw, on what machine, and what it cost you"
       <command> | a2aagentphone-note "one-line summary"

Worth including: what you observed rather than what you concluded, how to
reproduce it elsewhere, and what it cost -- money, minutes, a broken deploy.
That last one is what decides whether it gets fixed now.

Read the mailbox with:  a2aagentphone-note --read
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] in ("-h", "--help"):
        # Not politeness: with no TTY and any argument at all, everything below
        # takes its input as note text -- so `--help` filed itself as a note,
        # twice in the same mailbox on 2026-09-22, both times by someone
        # looking for how to read it. The channel for findings is the one thing
        # that must not fill up with noise from people trying to use it.
        print(USAGE)
        return 0
    if args and args[0] == "--read":
        print(MAILBOX.read_text(encoding="utf-8") if MAILBOX.exists()
              else f"(mailbox empty: {MAILBOX})")
        return 0
    if not args and sys.stdin.isatty():
        print(USAGE)
        return 0

    piped = "" if sys.stdin.isatty() else sys.stdin.read().strip()

    try:
        MAILBOX.parent.mkdir(parents=True, exist_ok=True)
        if not MAILBOX.exists():
            MAILBOX.write_text(
                "# a2aagentphone — mailbox\n\nNotes from agents on this machine. "
                "Append only.\n",
                encoding="utf-8",
            )
            # World-writable by design: any agent on the box may leave a note,
            # and none of them should need sudo to say something. Nothing
            # secret goes in here.
            MAILBOX.chmod(0o666)

        entry = [
            "\n---\n",
            f"## {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
            f" — {os.environ.get('USER') or os.getuid()} on {socket.gethostname()}\n",
            f"a2aagentphone {__version__}\n",
        ]
        if args:
            entry.append(" ".join(args) + "\n")
        if piped:
            # Verbatim and fenced: output is the evidence, and paraphrased
            # output is not evidence.
            entry.append(f"\n```\n{piped}\n```\n")
        with MAILBOX.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(entry))
    except OSError as e:
        print(f"a2aagentphone-note: cannot write {MAILBOX}: {e}", file=sys.stderr)
        return 1

    print(f"a2aagentphone: noted in {MAILBOX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
