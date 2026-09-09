"""Tests for universe.py's cache ownership model.

The scan owns universe.json (refresh=True: rebuild-if-stale-and-save, the
default). The read-only path (refresh=False) never writes the file, tolerates
a stale cache (flagging it), and falls back to an in-memory build (also
unwritten) if the cache is missing entirely. universe.py is ported unchanged
from the previous scanner, so this covers both paths even though the scan
only takes the first.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import universe


FAKE_UNIVERSE = [{"ticker": "TEST", "name": "Test Corp", "exchange": None,
                   "sector": "Technology", "market_cap": 5e10}]


class GetUniverseSingleWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_cache_path = universe.CACHE_PATH
        universe.CACHE_PATH = Path(self._tmp.name) / "universe.json"

        self._orig_build_universe = universe.build_universe
        self._build_calls = 0

        def _fake_build_universe(min_market_cap):
            self._build_calls += 1
            return list(FAKE_UNIVERSE), "nasdaq_screener"

        universe.build_universe = _fake_build_universe

    def tearDown(self):
        universe.CACHE_PATH = self._orig_cache_path
        universe.build_universe = self._orig_build_universe
        self._tmp.cleanup()

    def _write_cache(self, age_days: float, universe_data=None) -> None:
        built_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        universe.CACHE_PATH.write_text(json.dumps({
            "built_at": built_at.isoformat(),
            "source": "nasdaq_screener",
            "count": len(universe_data or FAKE_UNIVERSE),
            "universe": universe_data or FAKE_UNIVERSE,
        }))

    def _cache_mtime(self) -> float:
        return universe.CACHE_PATH.stat().st_mtime

    # --- refresh=True: rebuild-if-stale-and-save ---

    def test_refresh_true_uses_fresh_cache_without_rebuilding(self):
        self._write_cache(age_days=1)
        mtime_before = self._cache_mtime()

        result, source = universe.get_universe(10e9, refresh=True)

        self.assertEqual(self._build_calls, 0)
        self.assertEqual(source, "cache:nasdaq_screener")
        self.assertEqual(self._cache_mtime(), mtime_before)  # untouched

    def test_refresh_true_rebuilds_and_saves_when_cache_stale(self):
        self._write_cache(age_days=10)

        result, source = universe.get_universe(10e9, refresh=True)

        self.assertEqual(self._build_calls, 1)
        self.assertEqual(source, "nasdaq_screener")
        # The file was overwritten with a fresh built_at.
        cached = json.loads(universe.CACHE_PATH.read_text())
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["built_at"])
        self.assertLess(age, timedelta(minutes=1))

    # --- refresh=False: read-only ---

    def test_refresh_false_uses_stale_cache_without_rewriting(self):
        self._write_cache(age_days=10)
        mtime_before = self._cache_mtime()
        raw_before = universe.CACHE_PATH.read_text()
        warnings: list[str] = []

        result, source = universe.get_universe(10e9, refresh=False, warnings=warnings)

        self.assertEqual(self._build_calls, 0)  # never rebuilds
        self.assertEqual(result, FAKE_UNIVERSE)
        self.assertEqual(self._cache_mtime(), mtime_before)  # file untouched
        self.assertEqual(universe.CACHE_PATH.read_text(), raw_before)
        self.assertTrue(any("10 day" in w or "stale" in w.lower() for w in warnings),
                        f"expected a stale-cache warning naming the age, got {warnings}")

    def test_refresh_false_fresh_cache_no_warning(self):
        self._write_cache(age_days=1)
        warnings: list[str] = []

        result, source = universe.get_universe(10e9, refresh=False, warnings=warnings)

        self.assertEqual(self._build_calls, 0)
        self.assertEqual(warnings, [])

    def test_refresh_false_missing_cache_builds_in_memory_without_writing(self):
        self.assertFalse(universe.CACHE_PATH.exists())
        warnings: list[str] = []

        result, source = universe.get_universe(10e9, refresh=False, warnings=warnings)

        self.assertEqual(self._build_calls, 1)  # built in memory
        self.assertEqual(result, FAKE_UNIVERSE)
        self.assertFalse(universe.CACHE_PATH.exists())  # never written
        self.assertTrue(any("missing" in w.lower() for w in warnings),
                        f"expected a missing-cache warning, got {warnings}")

    def test_refresh_false_never_creates_cache_file_regardless_of_path(self):
        # Belt-and-suspenders: after a refresh=False call with no pre-existing
        # cache, the cache path must still not exist afterward.
        universe.get_universe(10e9, refresh=False, warnings=[])
        self.assertFalse(universe.CACHE_PATH.exists())


if __name__ == "__main__":
    unittest.main()
