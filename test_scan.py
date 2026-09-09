"""Tests for scan.py: the sleep-to-target, the guard/idempotency skip
reasons, the single 09:30-09:45 opening-range capture, and the reversal
flag. Deliberately scoped to the pure, testable pieces -- main() does live
network I/O and is not covered here.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from datetime import date, datetime, time as dtime
from pathlib import Path
from tempfile import TemporaryDirectory

import scan


def _bar(day: date, time_str: str, o: float, h: float, l: float, c: float,
         v: int = 0) -> dict:
    """A bar in the shape scan.py expects, timestamped in ET (fixed -04:00
    EDT offset -- every test date below is in August/September, so no DST
    edge case applies)."""
    return {"t": f"{day.isoformat()}T{time_str}-04:00",
            "o": o, "h": h, "l": l, "c": c, "v": v}


class TargetDatetimeTests(unittest.TestCase):
    def test_target_is_today_at_09_45_30_et(self):
        now = datetime(2026, 8, 18, 8, 0, 0, tzinfo=scan.ET)
        self.assertEqual(scan.target_datetime(now),
                         datetime(2026, 8, 18, 9, 45, 30, tzinfo=scan.ET))


class SecondsUntilTargetTests(unittest.TestCase):
    def test_positive_wait_before_target(self):
        now = datetime(2026, 8, 18, 9, 23, 0, tzinfo=scan.ET)  # the cron start
        self.assertAlmostEqual(scan.seconds_until_target(now),
                               22 * 60 + 30, delta=1)

    def test_negative_wait_after_target(self):
        # A late start must yield a negative wait (capture immediately), not
        # a rejection.
        now = datetime(2026, 8, 18, 10, 13, 0, tzinfo=scan.ET)
        self.assertLess(scan.seconds_until_target(now), 0)

    def test_zero_at_exact_target(self):
        now = datetime(2026, 8, 18, 9, 45, 30, tzinfo=scan.ET)
        self.assertEqual(scan.seconds_until_target(now), 0)


class SafetyCapTests(unittest.TestCase):
    """MAX_SLEEP_SECONDS must clear the wait the crons actually produce, and
    must sit inside the workflow's own timeout so a wait that cannot finish
    fails with a real message instead of a runner kill."""

    def test_cron_start_wait_is_within_cap(self):
        # The crons start the job at 08:35 ET: a 70.5 minute wait.
        now = datetime(2026, 8, 18, 8, 35, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now)
        self.assertAlmostEqual(wait, 70.5 * 60, delta=1)
        self.assertLessEqual(wait, scan.MAX_SLEEP_SECONDS)

    def test_external_trigger_start_wait_is_within_cap(self):
        # The external daily trigger starts it at ~09:15 ET.
        now = datetime(2026, 8, 18, 9, 15, 0, tzinfo=scan.ET)
        self.assertLessEqual(scan.seconds_until_target(now),
                             scan.MAX_SLEEP_SECONDS)

    def test_a_delayed_cron_start_is_still_absorbed(self):
        # The point of starting at 08:35: GitHub routinely delivers the event
        # 18-29 minutes late even when healthy. A 29 minute delay still lands
        # well inside the cap, and the sleep still hits the target exactly.
        now = datetime(2026, 8, 18, 9, 4, 0, tzinfo=scan.ET)
        self.assertLessEqual(scan.seconds_until_target(now),
                             scan.MAX_SLEEP_SECONDS)
        self.assertGreater(scan.seconds_until_target(now), 0)

    def test_cap_fits_inside_the_workflow_timeout(self):
        # Cross-file invariant. A sleep longer than the job's timeout is not a
        # longer wait, it is a job killed mid-sleep with a runner timeout
        # message that says nothing about the cause. The cap must fail fast
        # first, with room left for pip install, the universe fetch and the
        # capture.
        workflow = (Path(__file__).parent / ".github" / "workflows" /
                    "scan.yml").read_text(encoding="utf-8")
        timeout_minutes = int(re.search(r"^\s*timeout-minutes:\s*(\d+)\s*$",
                                        workflow, re.M).group(1))
        headroom = timeout_minutes * 60 - scan.MAX_SLEEP_SECONDS
        self.assertGreaterEqual(
            headroom, 10 * 60,
            f"MAX_SLEEP_SECONDS leaves only {headroom / 60:.0f} min of the "
            f"{timeout_minutes} min job timeout for the capture itself")

    def test_whole_guard_window_is_now_reachable(self):
        # With the cap at 75 minutes, every start the guard admits (08:30 ET
        # onward, a 75.5 minute wait at the very edge) is within a minute of
        # reachable, so a legal start no longer fails itself.
        now = datetime(2026, 8, 18, 8, 30, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now)
        self.assertAlmostEqual(wait, 75.5 * 60, delta=1)
        self.assertLess(wait - scan.MAX_SLEEP_SECONDS, 60)


class SkipReasonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_data_dir = scan.DATA_DIR
        scan.DATA_DIR = Path(self._tmp.name)

    def tearDown(self):
        scan.DATA_DIR = self._orig_data_dir
        self._tmp.cleanup()

    def _write_latest(self, session_date: str) -> None:
        scan.output_path().write_text(json.dumps({"session_date": session_date}))

    def test_weekend(self):
        now = datetime(2026, 8, 15, 9, 40, 0, tzinfo=scan.ET)  # Saturday
        self.assertEqual(scan.skip_reason(now), scan.SKIP_WEEKEND)

    def test_holiday(self):
        now = datetime(2026, 9, 7, 9, 40, 0, tzinfo=scan.ET)  # Labor Day 2026
        self.assertEqual(scan.skip_reason(now), scan.SKIP_HOLIDAY)

    def test_before_guard_window(self):
        # The EST-season cron fires at 08:23 ET every winter morning. This is
        # its exit, and it must stay a deliberate no-op.
        now = datetime(2026, 8, 18, 8, 23, 0, tzinfo=scan.ET)
        self.assertEqual(scan.skip_reason(now), scan.SKIP_OUTSIDE_GUARD)

    def test_after_guard_window(self):
        now = datetime(2026, 8, 18, 14, 30, 0, tzinfo=scan.ET)
        self.assertEqual(scan.skip_reason(now), scan.SKIP_OUTSIDE_GUARD)

    def test_within_window_and_no_prior_data_proceeds(self):
        now = datetime(2026, 8, 18, 9, 23, 0, tzinfo=scan.ET)
        self.assertIsNone(scan.skip_reason(now))
        self.assertTrue(scan.should_run(now))

    def test_late_start_within_window_still_proceeds(self):
        now = datetime(2026, 8, 18, 11, 12, 0, tzinfo=scan.ET)
        self.assertIsNone(scan.skip_reason(now))

    def test_idempotent_when_latest_json_matches_today(self):
        # The wrong-season cron at 10:23 ET in EDT lands here, once the
        # 09:23 run has pushed today's file.
        now = datetime(2026, 8, 18, 10, 23, 0, tzinfo=scan.ET)
        self._write_latest("2026-08-18")
        self.assertEqual(scan.skip_reason(now), scan.SKIP_ALREADY_CAPTURED)

    def test_proceeds_when_latest_json_is_stale(self):
        now = datetime(2026, 8, 18, 9, 23, 0, tzinfo=scan.ET)
        self._write_latest("2026-08-14")
        self.assertIsNone(scan.skip_reason(now))

    def test_non_dict_json_is_treated_as_no_prior_data(self):
        # A corrupted-but-valid-JSON output file must not crash the guard.
        now = datetime(2026, 8, 18, 9, 23, 0, tzinfo=scan.ET)
        scan.output_path().write_text(json.dumps(["not", "a", "dict"]))
        self.assertIsNone(scan.skip_reason(now))


class WriteStatusTests(unittest.TestCase):
    """scan.py's exit reason is what lets verify.py tell a deliberate no-op
    from a silent one."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._path = Path(self._tmp.name) / "nested" / "status.json"
        self._orig = os.environ.get(scan.STATUS_ENV_VAR)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop(scan.STATUS_ENV_VAR, None)
        else:
            os.environ[scan.STATUS_ENV_VAR] = self._orig
        self._tmp.cleanup()

    def test_writes_status_and_reason(self):
        os.environ[scan.STATUS_ENV_VAR] = str(self._path)
        scan.write_status(scan.STATUS_SKIPPED, scan.SKIP_HOLIDAY)
        payload = json.loads(self._path.read_text())
        self.assertEqual(payload["status"], scan.STATUS_SKIPPED)
        self.assertEqual(payload["reason"], scan.SKIP_HOLIDAY)

    def test_no_status_file_written_when_env_var_unset(self):
        os.environ.pop(scan.STATUS_ENV_VAR, None)
        scan.write_status(scan.STATUS_CAPTURED, "wrote data/latest.json")
        self.assertFalse(self._path.exists())


class IsReversalTests(unittest.TestCase):
    """reversal = sign(move_or) opposes sign(gap_pct), and abs(move_or)
    clears REVERSAL_THRESHOLD."""

    def test_gap_up_then_sells_off_past_threshold(self):
        self.assertTrue(scan.is_reversal(move_or=-0.03, gap_pct=0.05))

    def test_gap_down_then_rallies_past_threshold(self):
        # The sign case that matters most: a gap DOWN that rallies through
        # its opening range is just as much a failed gap as the inverse.
        self.assertTrue(scan.is_reversal(move_or=0.025, gap_pct=-0.05))

    def test_gap_up_that_keeps_going_up_is_not_a_reversal(self):
        self.assertFalse(scan.is_reversal(move_or=0.03, gap_pct=0.05))

    def test_gap_down_that_keeps_going_down_is_not_a_reversal(self):
        self.assertFalse(scan.is_reversal(move_or=-0.03, gap_pct=-0.05))

    def test_opposite_sign_below_threshold_is_noise(self):
        self.assertFalse(scan.is_reversal(move_or=-0.01, gap_pct=0.05))
        self.assertFalse(scan.is_reversal(move_or=0.01, gap_pct=-0.05))

    def test_exactly_at_threshold_counts(self):
        self.assertTrue(scan.is_reversal(move_or=-scan.REVERSAL_THRESHOLD,
                                         gap_pct=0.05))

    def test_missing_move_is_not_a_reversal(self):
        self.assertFalse(scan.is_reversal(move_or=None, gap_pct=0.05))

    def test_threshold_is_two_percent(self):
        self.assertEqual(scan.REVERSAL_THRESHOLD, 0.02)


class AnalyseTests(unittest.TestCase):
    def setUp(self):
        self.prior = date(2026, 8, 17)
        self.session = date(2026, 8, 18)
        self.meta = {"ticker": "TEST", "name": "Test Corp"}

    def _prior_bar(self):
        return _bar(self.prior, "15:55:00", 100, 100.5, 99.5, 100.0)

    def test_computes_gap_and_full_fifteen_minute_move(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 106, 104.8, 105.5),
            _bar(self.session, "09:38:00", 105.5, 107.5, 105.0, 107.0),
            _bar(self.session, "09:44:00", 107.0, 107.2, 106.0, 106.4),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["gap_pct"], (105 / 100) - 1, places=5)
        # move_or spans the whole window: 09:30 open -> the 09:44 bar's close.
        self.assertAlmostEqual(row["move_or"], (106.4 / 105) - 1, places=5)
        self.assertEqual(row["open_930"], 105.0)
        self.assertEqual(row["or_close"], 106.4)
        self.assertEqual(row["or_high"], 107.5)
        self.assertEqual(row["or_low"], 104.8)
        self.assertAlmostEqual(row["or_range_pct"], (107.5 - 104.8) / 105,
                               places=5)

    def test_window_is_half_open_at_09_45(self):
        # 09:44:00 is inside the window; 09:45:00 is not. A bar landing
        # exactly on the boundary must not extend the opening range.
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 105.2, 104.9, 105.0),
            _bar(self.session, "09:44:00", 105.0, 105.4, 104.9, 105.3),
            _bar(self.session, "09:45:00", 105.3, 120.0, 105.3, 119.0),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        self.assertEqual(row["or_close"], 105.3)
        self.assertEqual(row["or_high"], 105.4)

    def test_gap_up_that_sells_off_flags_reversal(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 106, 104.8, 105.5),  # +5% gap
            _bar(self.session, "09:44:00", 105.5, 105.5, 100.0, 100.8),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        self.assertGreater(row["gap_pct"], 0)
        self.assertLess(row["move_or"], -scan.REVERSAL_THRESHOLD)
        self.assertTrue(row["reversal"])

    def test_gap_down_that_rallies_flags_reversal(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 92, 92.2, 91.0, 91.5),  # -8% gap
            _bar(self.session, "09:44:00", 91.5, 96.0, 91.5, 95.5),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        self.assertLess(row["gap_pct"], 0)
        self.assertGreater(row["move_or"], scan.REVERSAL_THRESHOLD)
        self.assertTrue(row["reversal"])

    def test_gap_up_that_holds_does_not_flag_reversal(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 106, 104.8, 105.5),
            _bar(self.session, "09:44:00", 105.5, 108, 105.4, 107.9),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        self.assertFalse(row["reversal"])

    def test_no_segment_fields_remain(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 106, 104.8, 105.5),
            _bar(self.session, "09:44:00", 105.5, 106, 105.0, 105.9),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        for gone in ("price_0932", "move_since_alert", "price_0945",
                     "move_early", "move_late", "reversal_basis", "or_move"):
            self.assertNotIn(gone, row)

    def test_premarket_fields(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "07:15:00", 103, 103.5, 102.8, 103.2),
            _bar(self.session, "09:30:00", 105, 106, 104.8, 105.5),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        self.assertEqual(row["premkt_last"], 103.2)
        self.assertEqual(row["premkt_bar_count"], 1)
        self.assertAlmostEqual(row["premkt_move"], (103.2 / 100) - 1, places=5)

    def test_no_prior_close_bars_returns_none(self):
        bars = [_bar(self.session, "09:30:00", 105, 106, 104.8, 105.5)]
        self.assertIsNone(scan.analyse(self.meta, bars, self.session, self.prior))

    def test_no_opening_range_bars_returns_none(self):
        # A name whose first print lands after 09:45 has no opening range and
        # is absent from the output rather than half-populated.
        bars = [self._prior_bar(),
                _bar(self.session, "09:52:00", 105, 106, 104.8, 105.5)]
        self.assertIsNone(scan.analyse(self.meta, bars, self.session, self.prior))


class WindowLabelTests(unittest.TestCase):
    """Labels must be derived from the same time constants the pipeline
    computes with -- never a hardcoded string that can drift from them."""

    def test_or_window_label(self):
        self.assertEqual(scan.or_window_label(), "09:30-09:45 ET")

    def test_windows_keys(self):
        self.assertEqual(set(scan.build_windows()),
                         {"gap", "opening_range", "reversal"})

    def test_window_labels(self):
        windows = scan.build_windows()
        self.assertEqual(windows["gap"], "prior close 16:00 ET -> 09:30 open")
        self.assertEqual(windows["opening_range"], "09:30 -> 09:45 ET")
        self.assertEqual(windows["reversal"], "09:30 -> 09:45 ET")

    def test_labels_track_the_constant(self):
        # Move the constant and the labels must follow, both of them.
        original = scan.OR_CLOSE_TIME
        try:
            scan.OR_CLOSE_TIME = dtime(9, 41)
            self.assertEqual(scan.or_window_label(), "09:30-09:41 ET")
            windows = scan.build_windows()
            self.assertEqual(windows["opening_range"], "09:30 -> 09:41 ET")
            self.assertEqual(windows["reversal"], "09:30 -> 09:41 ET")
        finally:
            scan.OR_CLOSE_TIME = original


class SortMoversTests(unittest.TestCase):
    """Sorting is a guarantee of the data layer, not an instruction left for
    a downstream consumer -- e.g. the email prompt -- to infer or enforce."""

    def test_sorts_by_abs_gap_pct_descending(self):
        rows = [
            {"ticker": "A", "gap_pct": 0.06},
            {"ticker": "B", "gap_pct": -0.11},
            {"ticker": "C", "gap_pct": 0.08},
            {"ticker": "D", "gap_pct": -0.05},
        ]
        result = scan.sort_movers(rows)
        self.assertEqual([r["ticker"] for r in result], ["B", "C", "A", "D"])


class SingleModeTests(unittest.TestCase):
    """Regression guard: the dual-mode machinery is gone and must not come
    back by way of a partial re-port."""

    def test_removed_names_are_absent(self):
        for gone in ("RUN_MODE", "MODE_TARGET_TIME", "MODE_OUTPUT_FILENAMES",
                     "MODE_OR_WINDOW_LABEL", "MODE_OR_CLOSE_TIME",
                     "SLEEP_TARGET_TIME", "RANGE_TARGET_TIME",
                     "ALERT_SNAPSHOT_TIME", "SEGMENT_SPLIT_TIME",
                     "mode_output_path", "universe_for_mode"):
            self.assertFalse(hasattr(scan, gone), f"{gone} still exists")

    def test_single_output_path(self):
        self.assertEqual(scan.output_path(), scan.DATA_DIR / "latest.json")

    def test_capture_target_is_09_45_30(self):
        self.assertEqual(scan.CAPTURE_TARGET_TIME, dtime(9, 45, 30))

    def test_min_gap_default_is_five_percent(self):
        # Read from the source rather than the imported module, which honours
        # a MIN_GAP already in the environment.
        source = Path(__file__).with_name("scan.py").read_text(encoding="utf-8")
        self.assertIn('float(os.environ.get("MIN_GAP", 0.05))', source)


if __name__ == "__main__":
    unittest.main()
