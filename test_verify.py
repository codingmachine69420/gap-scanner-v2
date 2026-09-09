"""Tests for verify.py -- the freshness assertion.

The distinction under test is the one the old repo never made: a run that
skipped on purpose stays green, a run that produced no fresh data goes red.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import scan
import verify

# The capture instant on the session under test, and a moment just after it.
TARGET = datetime(2026, 9, 9, 9, 45, 30, tzinfo=verify.ET)
NOW = TARGET + timedelta(seconds=10)


def _payload(session_date="2026-09-09", as_of=None) -> dict:
    return {"session_date": session_date,
            "as_of": (as_of or TARGET).isoformat()}


def _captured() -> dict:
    return {"status": scan.STATUS_CAPTURED, "reason": "wrote data/latest.json",
            "session_date": "2026-09-09"}


class DeliberateNoOpStaysGreenTests(unittest.TestCase):
    def test_every_skip_reason_exits_zero_without_touching_the_data(self):
        for reason in (scan.SKIP_WEEKEND, scan.SKIP_HOLIDAY,
                       scan.SKIP_OUTSIDE_GUARD, scan.SKIP_ALREADY_CAPTURED):
            with self.subTest(reason=reason):
                status = {"status": scan.STATUS_SKIPPED, "reason": reason}
                # Stale data on disk, deliberately: a skip must not assert on it.
                code, message = verify.check(status, _payload("2026-08-14"), NOW)
                self.assertEqual(code, 0, message)
                self.assertIn(reason, message)

    def test_skip_is_green_even_with_no_committed_data_at_all(self):
        status = {"status": scan.STATUS_SKIPPED, "reason": scan.SKIP_HOLIDAY}
        code, _ = verify.check(status, None, NOW)
        self.assertEqual(code, 0)


class SilentNoOpGoesRedTests(unittest.TestCase):
    def test_missing_status_file_is_red(self):
        # scan.py exited 0 without recording why. We cannot tell a skip from
        # a failure, and "cannot tell" is the failure this step exists to end.
        code, message = verify.check(None, _payload(), NOW)
        self.assertEqual(code, 1)
        self.assertIn("status", message.lower())

    def test_unrecognised_status_is_red(self):
        code, _ = verify.check({"status": "something_else", "reason": "?"},
                               _payload(), NOW)
        self.assertEqual(code, 1)

    def test_captured_but_no_committed_data_is_red(self):
        code, message = verify.check(_captured(), None, NOW)
        self.assertEqual(code, 1)
        self.assertIn("latest.json", message)

    def test_captured_but_session_date_is_yesterday_is_red(self):
        code, message = verify.check(_captured(), _payload("2026-09-08"), NOW)
        self.assertEqual(code, 1)
        self.assertIn("2026-09-08", message)   # names the value found
        self.assertIn("2026-09-09", message)   # and the value expected

    def test_the_late_delivery_case_is_red(self):
        # The failure this assertion exists for. GitHub delivers the schedule
        # three hours late; the job starts at 12:47, fetches the 09:30-09:45
        # bars -- historical, and entirely correct -- and commits them under
        # today's session_date. Measured against "now" this looks perfectly
        # fresh. It is useless: the 09:50 reader fired three hours earlier on
        # yesterday's file. It must be red.
        late = _payload(as_of=datetime(2026, 9, 9, 12, 47, 0, tzinfo=verify.ET))
        now = datetime(2026, 9, 9, 12, 47, 30, tzinfo=verify.ET)
        code, message = verify.check(_captured(), late, now)
        self.assertEqual(code, 1)
        self.assertIn(late["as_of"], message)
        self.assertIn("181.5 minutes", message)   # 09:45:30 -> 12:47:00

    def test_captured_but_as_of_is_hours_before_the_target_is_red(self):
        stale = _payload(as_of=NOW - timedelta(hours=3))
        code, message = verify.check(_captured(), stale, NOW)
        self.assertEqual(code, 1)
        self.assertIn(stale["as_of"], message)
        self.assertIn("179.8 minutes", message)

    def test_captured_but_as_of_is_far_after_the_target_is_red(self):
        code, _ = verify.check(_captured(),
                               _payload(as_of=NOW + timedelta(hours=2)), NOW)
        self.assertEqual(code, 1)

    def test_unparseable_as_of_is_red(self):
        code, message = verify.check(_captured(),
                                     {"session_date": "2026-09-09",
                                      "as_of": "not a timestamp"}, NOW)
        self.assertEqual(code, 1)
        self.assertIn("as_of", message)

    def test_missing_as_of_is_red(self):
        code, _ = verify.check(_captured(), {"session_date": "2026-09-09"}, NOW)
        self.assertEqual(code, 1)


class FreshCaptureIsGreenTests(unittest.TestCase):
    def test_captured_at_the_target_instant(self):
        code, message = verify.check(_captured(), _payload(), NOW)
        self.assertEqual(code, 0, message)

    def test_a_slow_bar_fetch_still_passes(self):
        # The healthy drift: the sleep wakes at 09:45:30 and the fetch, the
        # earnings join and the write take a couple of minutes.
        slow = _payload(as_of=TARGET + timedelta(minutes=2))
        code, message = verify.check(_captured(), slow,
                                     TARGET + timedelta(minutes=3))
        self.assertEqual(code, 0, message)

    def test_just_inside_the_limit(self):
        limit = verify.MAX_AS_OF_DRIFT_SECONDS
        fresh = _payload(as_of=TARGET + timedelta(seconds=limit - 1))
        self.assertEqual(verify.check(_captured(), fresh, NOW)[0], 0)

    def test_just_outside_the_limit(self):
        limit = verify.MAX_AS_OF_DRIFT_SECONDS
        for offset in (limit + 1, -(limit + 1)):
            with self.subTest(offset=offset):
                stale = _payload(as_of=TARGET + timedelta(seconds=offset))
                self.assertEqual(verify.check(_captured(), stale, NOW)[0], 1)

    def test_naive_as_of_is_read_as_eastern(self):
        naive = {"session_date": "2026-09-09",
                 "as_of": NOW.replace(tzinfo=None).isoformat()}
        self.assertEqual(verify.check(_captured(), naive, NOW)[0], 0)

    def test_limit_is_fifteen_minutes(self):
        self.assertEqual(verify.MAX_AS_OF_DRIFT_SECONDS, 15 * 60)


class TargetIsDerivedFromScanTests(unittest.TestCase):
    """The assertion's target and the capture's target are one constant. If
    they could drift, this step would eventually be asserting against a time
    the scanner no longer captures at -- the same class of bug as a window
    label hardcoded next to the code that computes it."""

    def test_target_is_scans_capture_time_on_the_session(self):
        self.assertEqual(verify.capture_target(date(2026, 9, 9)),
                         datetime(2026, 9, 9, 9, 45, 30, tzinfo=verify.ET))
        self.assertEqual(verify.capture_target(date(2026, 9, 9)).timetz().replace(tzinfo=None),
                         scan.CAPTURE_TARGET_TIME)

    def test_moving_the_capture_moves_the_assertion(self):
        original = scan.CAPTURE_TARGET_TIME
        try:
            scan.CAPTURE_TARGET_TIME = dtime(10, 15, 0)
            # as_of at the old target is now three-quarters of an hour out.
            self.assertEqual(verify.check(_captured(), _payload(), NOW)[0], 1)
            # as_of at the new target passes.
            moved = _payload(as_of=datetime(2026, 9, 9, 10, 15, 5,
                                            tzinfo=verify.ET))
            self.assertEqual(verify.check(_captured(), moved, NOW)[0], 0)
        finally:
            scan.CAPTURE_TARGET_TIME = original


class ReadStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig = os.environ.get(scan.STATUS_ENV_VAR)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop(scan.STATUS_ENV_VAR, None)
        else:
            os.environ[scan.STATUS_ENV_VAR] = self._orig
        self._tmp.cleanup()

    def test_round_trips_what_scan_wrote(self):
        path = Path(self._tmp.name) / "status.json"
        os.environ[scan.STATUS_ENV_VAR] = str(path)
        scan.write_status(scan.STATUS_SKIPPED, scan.SKIP_OUTSIDE_GUARD)
        self.assertEqual(verify.read_status()["reason"], scan.SKIP_OUTSIDE_GUARD)

    def test_unset_env_var_reads_as_no_status(self):
        os.environ.pop(scan.STATUS_ENV_VAR, None)
        self.assertIsNone(verify.read_status())

    def test_missing_file_reads_as_no_status(self):
        os.environ[scan.STATUS_ENV_VAR] = str(Path(self._tmp.name) / "nope.json")
        self.assertIsNone(verify.read_status())

    def test_corrupt_file_reads_as_no_status(self):
        path = Path(self._tmp.name) / "status.json"
        path.write_text("{ not json")
        os.environ[scan.STATUS_ENV_VAR] = str(path)
        self.assertIsNone(verify.read_status())

    def test_non_dict_file_reads_as_no_status(self):
        path = Path(self._tmp.name) / "status.json"
        path.write_text(json.dumps(["nope"]))
        os.environ[scan.STATUS_ENV_VAR] = str(path)
        self.assertIsNone(verify.read_status())


if __name__ == "__main__":
    unittest.main()
