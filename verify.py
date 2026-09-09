"""
Freshness assertion. Runs inside the scan job, after the commit step.

The old scanner spent three months producing green runs and no data, because
nothing ever checked that a successful run had actually committed anything.
This is that check, and the only thing this repo does about failure: it makes
the run red. It does not retry, heal, dispatch, or notify.

The distinction it has to get right:

  deliberate no-op  -> green. scan.py exited early on purpose: weekend,
                       market holiday, outside the guard window, or today's
                       capture was already committed by the other cron. There
                       is no fresh data because there was nothing to capture.

  silent no-op      -> RED. scan.py said it captured, but the committed
                       data/latest.json is not today's session, or was not
                       captured at the capture instant. Also red if the status
                       file is missing or unreadable, since then we cannot
                       tell which case we are in -- and "cannot tell" is
                       exactly the failure mode this whole step exists to end.

as_of is measured against 09:45:30 ET on session_date, not against the moment
this check runs. The difference matters, and it is the whole point: the old
scanner's schedule started arriving hours late, so a run would begin at 12:47,
fetch the 09:30-09:45 bars -- which are historical, and correct -- and commit
them. Checked against "now", that run looks perfectly fresh. It is not. The
09:50 ET reader had already fired three hours earlier on yesterday's file, so
correct data delivered at 12:47 is worth exactly nothing. Anchoring to the
capture target is what makes that failure visible instead of green.

The target is taken from scan.CAPTURE_TARGET_TIME, so moving the capture moves
this assertion with it and the two cannot drift apart.

scan.py writes its exit reason to $SCAN_STATUS_FILE; this reads it. The data
is read from the commit (git show HEAD:...), not the working tree, so the
assertion is about what actually landed in the repo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import scan

ET = ZoneInfo("America/New_York")

# How far as_of may sit from the capture target before the run goes red. The
# capture is a sleep-to-target, so in a healthy run the drift is seconds: this
# is slack for a slow runner and a slow bar fetch, not a tolerance for late
# delivery. A run that captures outside 09:30:30-10:00:30 ET has missed the
# reader it exists to feed.
MAX_AS_OF_DRIFT_SECONDS = 15 * 60

COMMITTED_DATA_REF = "HEAD:data/latest.json"


def capture_target(session: date) -> datetime:
    """The instant the capture was supposed to happen on that session.
    Derived from scan.py's own constant so the two cannot drift."""
    return datetime.combine(session, scan.CAPTURE_TARGET_TIME, ET)


def read_status() -> dict | None:
    """The exit reason scan.py recorded, or None if it recorded nothing."""
    dest = os.environ.get(scan.STATUS_ENV_VAR)
    if not dest:
        return None
    try:
        payload = json.loads(Path(dest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_committed_payload() -> dict | None:
    """data/latest.json as it exists in the current commit."""
    try:
        raw = subprocess.run(["git", "show", COMMITTED_DATA_REF],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def check(status: dict | None, payload: dict | None,
          now_et: datetime) -> tuple[int, str]:
    """Pure decision: (exit code, message). Everything above is the I/O."""
    if status is None:
        return 1, ("No status file from scan.py. A run that neither captured "
                   "nor recorded a reason to skip is a silent no-op.")

    kind = status.get("status")
    reason = status.get("reason")

    if kind == scan.STATUS_SKIPPED:
        return 0, f"scan.py skipped deliberately ({reason}). Nothing to assert."

    if kind != scan.STATUS_CAPTURED:
        return 1, (f"Unrecognised scan status {kind!r} (reason {reason!r}). "
                   "Treating as a silent no-op.")

    if payload is None:
        return 1, ("scan.py reported a capture, but data/latest.json is "
                   f"missing or unreadable at {COMMITTED_DATA_REF}.")

    today = now_et.strftime("%Y-%m-%d")
    session_date = payload.get("session_date")
    as_of_raw = payload.get("as_of")

    if session_date != today:
        return 1, (f"Committed data is not today's session. "
                   f"session_date={session_date!r}, today in America/New_York "
                   f"is {today!r}.")

    try:
        as_of = datetime.fromisoformat(as_of_raw)
    except (TypeError, ValueError):
        return 1, f"Committed data has an unparseable as_of: {as_of_raw!r}."

    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ET)

    # session_date == today was established above, so this parse cannot fail.
    target = capture_target(date.fromisoformat(session_date))
    drift = abs((as_of - target).total_seconds())
    if drift > MAX_AS_OF_DRIFT_SECONDS:
        return 1, (f"Committed data was captured at the wrong time of day. "
                   f"as_of={as_of_raw!r} is {drift / 60:.1f} minutes from the "
                   f"{target.isoformat()} capture target, limit is "
                   f"{MAX_AS_OF_DRIFT_SECONDS / 60:.0f} minutes. The bars may "
                   f"be correct, but they landed after the reader that needs "
                   f"them.")

    return 0, (f"Fresh: session_date={session_date}, as_of={as_of_raw}, "
               f"{drift / 60:.1f} minutes from the {target.isoformat()} "
               f"capture target.")


def main() -> int:
    code, message = check(read_status(), read_committed_payload(),
                          datetime.now(ET))
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
