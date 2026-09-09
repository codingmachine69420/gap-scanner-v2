"""
US large-cap gap scanner. One capture per trading day.

The job starts well before the capture instant and sleeps to a precise
wall-clock target, because GitHub Actions can delay a job's *start* by a long
way but cannot delay what the job does once it is running. Everything that
does not depend on market data (guard, universe fetch) happens before the
sleep, so the post-sleep path is just bars -> compute -> write.

Capture target: 09:45:30 ET. That yields the gap (prior close -> 09:30 open)
plus a complete 15-minute opening range (09:30 -> 09:45), from bars that have
had 15 minutes to print rather than 2.

Windows measured (all America/New_York):
  prior session close   15:50 - 16:00 on the prior trading day
  pre-market            04:00 - 09:30
  opening range         09:30 - 09:45

Output: data/latest.json, plus a dated archive copy. An empty movers list is a
VALID result, not an error.

Exit status is also written to $SCAN_STATUS_FILE (when set) so the workflow can
tell a deliberate no-op -- weekend, holiday, outside the guard window, already
captured today -- from a run that claimed to capture and did not. See verify.py.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from universe import get_universe

log = logging.getLogger("scan")

ET = ZoneInfo("America/New_York")
DATA_DIR = Path("data")
OUTPUT_FILENAME = "latest.json"

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nasdaq.com/",
}

# Tunables
MIN_MARKET_CAP = float(os.environ.get("MIN_MARKET_CAP", 10e9))
MIN_GAP = float(os.environ.get("MIN_GAP", 0.05))
BATCH_SIZE = 200

# The single capture instant. 09:45:30 rather than 09:45:00 so the last
# 09:44 one-minute bar is certain to have been published before we ask.
CAPTURE_TARGET_TIME = dtime(9, 45, 30)

# Named constants, not literals: build_windows(), the or_window label and
# analyse()'s own arithmetic all derive from these, so a window can never be
# described in the JSON as something different from what was computed.
MARKET_OPEN_TIME = dtime(9, 30)
OR_CLOSE_TIME = dtime(9, 45)
PRIOR_CLOSE_TIME = dtime(16, 0)

# A stock that gapped up and then sold off through its first 15 minutes is a
# failed gap; same in reverse for a gap down that rallies. The floor exists
# because small opposite-sign wiggles are noise on a thin IEX book, not a
# reversal.
REVERSAL_THRESHOLD = 0.02

# Wide guard window: this exists to catch the DST-mismatched cron and a badly
# delayed start, not to enforce precision. The sleep-to-target handles that.
GUARD_WINDOW_START = dtime(8, 30)
GUARD_WINDOW_END = dtime(14, 0)

# The crons start the job at ~08:35 ET, so the real wait is ~70 minutes; the
# external daily trigger starts it at ~09:15, a ~30 minute wait.
#
# The cap is set from the workflow's timeout-minutes (90), not from the guard
# window: a sleep longer than the job may live is not a longer wait, it is a
# job killed mid-sleep with a runner timeout message that says nothing about
# the cause. Failing fast at 75 minutes leaves 15 minutes for the pip install,
# the universe fetch and the capture itself, and says plainly in the log why
# it stopped. test_scan.py reads timeout-minutes out of the workflow and
# asserts the headroom, so the two cannot drift apart.
MAX_SLEEP_SECONDS = 75 * 60

# US market holidays. Extend annually -- deliberately explicit rather than a
# dependency, since the list is short and a stale holiday's failure mode is a
# harmless empty report.
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}

# ------------------------------------------------------------------- status
# Written to $SCAN_STATUS_FILE, read by verify.py. The whole point of the
# distinction: a run that skipped on purpose stays green; a run that claimed
# to capture but left stale data goes red.
STATUS_ENV_VAR = "SCAN_STATUS_FILE"
STATUS_CAPTURED = "captured"
STATUS_SKIPPED = "skipped"

SKIP_WEEKEND = "weekend"
SKIP_HOLIDAY = "market_holiday"
SKIP_OUTSIDE_GUARD = "outside_guard_window"
SKIP_ALREADY_CAPTURED = "already_captured_today"


def write_status(status: str, reason: str, session_date: str | None = None) -> None:
    """Record why this process is exiting, for the workflow's freshness
    assertion. A no-op when SCAN_STATUS_FILE is unset (local runs), so the
    status file never lands inside the repo by accident."""
    dest = os.environ.get(STATUS_ENV_VAR)
    if not dest:
        return
    payload = {"status": status, "reason": reason, "session_date": session_date}
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))
    log.info("Status: %s (%s)", status, reason)


# ---------------------------------------------------------------- scheduling

def output_path() -> Path:
    return DATA_DIR / OUTPUT_FILENAME


def already_ran_today(now_et: datetime) -> bool:
    """True if the committed output already covers today's ET session.

    Read from the checked-out repo, so this only sees a prior run's output
    once it has been committed and pushed. That is what makes the second,
    wrong-DST-season cron a cheap no-op instead of a duplicate capture.
    """
    path = output_path()
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("session_date") == now_et.strftime("%Y-%m-%d")


def skip_reason(now_et: datetime) -> str | None:
    """The reason this run should not capture, or None if it should.

    Every reason here is a deliberate no-op that must leave the workflow
    green: there is genuinely nothing to capture, or it has already been
    captured. Anything else that ends in no fresh data is a failure, and
    verify.py turns it red.
    """
    if now_et.weekday() >= 5:
        log.info("Weekend. Exiting.")
        return SKIP_WEEKEND
    if now_et.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        log.info("Market holiday. Exiting.")
        return SKIP_HOLIDAY
    if not (GUARD_WINDOW_START <= now_et.time() < GUARD_WINDOW_END):
        log.info("ET time is %s, outside the %s-%s guard window. Exiting.",
                 now_et.strftime("%H:%M"), GUARD_WINDOW_START, GUARD_WINDOW_END)
        return SKIP_OUTSIDE_GUARD
    if already_ran_today(now_et):
        log.info("Already have today's data. Exiting.")
        return SKIP_ALREADY_CAPTURED
    return None


def should_run(now_et: datetime) -> bool:
    return skip_reason(now_et) is None


def target_datetime(now_et: datetime) -> datetime:
    """Today's capture instant."""
    return datetime.combine(now_et.date(), CAPTURE_TARGET_TIME, ET)


def seconds_until_target(now_et: datetime) -> float:
    """Seconds from now_et to today's target. Negative if already past it."""
    return (target_datetime(now_et) - now_et).total_seconds()


def prior_trading_day(session: date) -> date:
    day = session - timedelta(days=1)
    while day.weekday() >= 5 or day.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        day -= timedelta(days=1)
    return day


# -------------------------------------------------------------------- alpaca

def alpaca_headers() -> dict:
    key = os.environ.get("ALPACA_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set. "
                 "Set them as GitHub Secrets, or in a local .env for testing.")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_bars(symbols: list[str], start: datetime, end: datetime,
               timeframe: str = "1Min") -> dict[str, list[dict]]:
    """Fetch bars for many symbols, following pagination.

    Free tier is the IEX feed only (~5-10% of consolidated volume). Prices are
    representative for large caps; volume figures are indicative at best.
    """
    headers = alpaca_headers()
    out: dict[str, list[dict]] = {}

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        # Alpaca uses dot notation for share classes (BRK.B); our universe
        # carries Nasdaq's slash notation (BRK/B). A literal "/" in the
        # symbols param 400s the ENTIRE batch, not just that one symbol, so
        # a single dual-class ticker sharing a batch with 199 others used to
        # take the whole batch down. Translate for the request only and
        # translate back on the way out -- safe because EXCLUDE_PATTERN
        # already keeps any "." out of every ticker in the universe.
        api_batch = [s.replace("/", ".") for s in batch]
        page_token = None
        while True:
            params = {
                "symbols": ",".join(api_batch),
                "timeframe": timeframe,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "adjustment": "raw",
                "feed": "iex",
            }
            if page_token:
                params["page_token"] = page_token

            payload = _get_with_retry(ALPACA_BARS_URL, params, headers)
            for symbol, bars in (payload.get("bars") or {}).items():
                out.setdefault(symbol.replace(".", "/"), []).extend(bars)

            page_token = payload.get("next_page_token")
            if not page_token:
                break

        log.info("Bars: %d/%d symbols fetched", min(i + BATCH_SIZE, len(symbols)),
                 len(symbols))
    return out


def _get_with_retry(url: str, params: dict, headers: dict,
                    retries: int = 3, timeout: int = 45) -> dict:
    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=timeout)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5))
                log.warning("Rate limited; sleeping %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2 ** attempt
            log.warning("Request failed (%s); retry in %ds", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Request failed after {retries} attempts: {last}")


# ------------------------------------------------------------------ earnings

def fetch_earnings(day: date, warnings: list[str]) -> dict[str, dict]:
    """Undocumented Nasdaq earnings calendar. Failure here degrades the report
    (movers lose their earnings tag) but must not kill the run."""
    try:
        payload = _get_with_retry(
            NASDAQ_EARNINGS_URL,
            {"date": day.strftime("%Y-%m-%d")},
            BROWSER_HEADERS,
            retries=2,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Earnings calendar unavailable for {day}: {exc}"
        log.warning(msg)
        warnings.append(msg)
        return {}

    data = payload.get("data") or {}
    rows = data.get("rows")
    if not isinstance(rows, list):
        warnings.append(f"Unexpected earnings payload shape for {day}")
        return {}

    if rows:
        log.info("Earnings row fields: %s", sorted(rows[0].keys()))

    result = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if symbol:
            result[symbol] = {
                "date": day.strftime("%Y-%m-%d"),
                "time_raw": row.get("time"),
                "eps_actual": row.get("eps"),
                "eps_estimate": row.get("epsForecast"),
                "surprise_pct": row.get("surprise"),
            }
    return result


def classify_timing(time_raw: str | None) -> str | None:
    if not time_raw:
        return None
    lowered = time_raw.lower()
    if "pre" in lowered or "before" in lowered:
        return "BMO"
    if "after" in lowered or "post" in lowered:
        return "AMC"
    return "UNKNOWN"


# ------------------------------------------------------------------ analysis

def _bar_timestamp(bar: dict) -> datetime:
    return datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(ET)


def bars_in_window(bars: list[dict], start: datetime, end: datetime) -> list[dict]:
    return [b for b in bars if start <= _bar_timestamp(b) < end]


def is_reversal(move_or: float | None, gap_pct: float) -> bool:
    """True when the 09:30-09:45 move runs opposite to the gap by at least
    REVERSAL_THRESHOLD -- a gap up that sold off through its opening range,
    or a gap down that rallied through it."""
    if move_or is None:
        return False
    if abs(move_or) < REVERSAL_THRESHOLD:
        return False
    return (move_or > 0) != (gap_pct > 0)


def analyse(ticker_meta: dict, bars: list[dict], session: date,
            prior: date) -> dict | None:
    open_930 = datetime.combine(session, MARKET_OPEN_TIME, ET)
    or_close_at = datetime.combine(session, OR_CLOSE_TIME, ET)
    premkt_start = datetime.combine(session, dtime(4, 0), ET)
    prior_close_start = datetime.combine(prior, dtime(15, 50), ET)
    prior_close_end = datetime.combine(prior, PRIOR_CLOSE_TIME, ET)

    prior_bars = bars_in_window(bars, prior_close_start, prior_close_end)
    premkt_bars = bars_in_window(bars, premkt_start, open_930)
    or_bars = bars_in_window(bars, open_930, or_close_at)

    if not prior_bars or not or_bars:
        return None

    prior_close = prior_bars[-1]["c"]
    if not prior_close:
        return None

    opening = or_bars[0]["o"]
    closing = or_bars[-1]["c"]
    highs = max(b["h"] for b in or_bars)
    lows = min(b["l"] for b in or_bars)

    gap_pct = (opening / prior_close) - 1
    move_or = (closing / opening) - 1

    return {
        **ticker_meta,
        "prior_close": round(prior_close, 4),
        "premkt_last": round(premkt_bars[-1]["c"], 4) if premkt_bars else None,
        "premkt_move": round((premkt_bars[-1]["c"] / prior_close) - 1, 5)
                       if premkt_bars else None,
        "premkt_bar_count": len(premkt_bars),
        "open_930": round(opening, 4),
        "or_close": round(closing, 4),
        "gap_pct": round(gap_pct, 5),
        "move_or": round(move_or, 5),
        "or_range_pct": round((highs - lows) / opening, 5),
        "or_high": round(highs, 4),
        "or_low": round(lows, 4),
        "volume_or": sum(b.get("v", 0) for b in or_bars),
        "reversal": is_reversal(move_or, gap_pct),
    }


def _fmt_time(t: dtime) -> str:
    return t.strftime("%H:%M")


def or_window_label() -> str:
    return f"{_fmt_time(MARKET_OPEN_TIME)}-{_fmt_time(OR_CLOSE_TIME)} ET"


def build_windows() -> dict[str, str]:
    """Explicit, human-readable window labels for the output JSON, derived
    from the same time constants the pipeline computes with -- never a
    separate hardcoded string that can silently drift from them. A downstream
    consumer (e.g. the email task) should read this dict rather than infer
    windows from field names or comments."""
    return {
        "gap": (f"prior close {_fmt_time(PRIOR_CLOSE_TIME)} ET -> "
                f"{_fmt_time(MARKET_OPEN_TIME)} open"),
        "opening_range": (f"{_fmt_time(MARKET_OPEN_TIME)} -> "
                          f"{_fmt_time(OR_CLOSE_TIME)} ET"),
        "reversal": (f"{_fmt_time(MARKET_OPEN_TIME)} -> "
                     f"{_fmt_time(OR_CLOSE_TIME)} ET"),
    }


def sort_movers(movers: list[dict]) -> list[dict]:
    """Movers must be sorted by abs(gap_pct) descending -- largest gaps
    first, regardless of sign. This is a guarantee of the data layer, not
    an instruction left for a downstream consumer to infer or enforce."""
    movers.sort(key=lambda r: abs(r["gap_pct"]), reverse=True)
    return movers


# ---------------------------------------------------------------------- main

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    warnings: list[str] = []
    now_et = datetime.now(ET)
    force = os.environ.get("FORCE_RUN") == "1"

    if not force:
        reason = skip_reason(now_et)
        if reason is not None:
            write_status(STATUS_SKIPPED, reason)
            return 0

    session = now_et.date()
    prior = prior_trading_day(session)
    log.info("Session %s (prior %s)", session, prior)

    universe, universe_source = get_universe(MIN_MARKET_CAP, warnings=warnings)
    meta_by_ticker = {u["ticker"]: u for u in universe}
    symbols = sorted(meta_by_ticker)

    # Sleep to the capture instant so the commit lands at the same moment
    # every trading day regardless of Actions' start-time jitter. now_et is
    # refreshed here rather than reused from above so time spent in the
    # universe fetch does not silently push the wake time past the target.
    if not force:
        now_et = datetime.now(ET)
        wait = seconds_until_target(now_et)
        if wait > MAX_SLEEP_SECONDS:
            log.error("Computed wait of %.0fs exceeds the %ds safety cap -- "
                      "the time logic looks wrong. Exiting.",
                      wait, MAX_SLEEP_SECONDS)
            return 1
        if wait > 0:
            mins, secs = divmod(int(round(wait)), 60)
            log.info("Waiting %dm %ds until %s ET", mins, secs,
                     CAPTURE_TARGET_TIME.strftime("%H:%M:%S"))
            time.sleep(wait)
        else:
            log.warning("Already %.0fs past the %s ET target (started late); "
                        "proceeding immediately.", -wait,
                        CAPTURE_TARGET_TIME.strftime("%H:%M:%S"))

    # Refreshed unconditionally so as_of reflects the real current time even
    # under FORCE_RUN=1, where the sleep block above is skipped entirely.
    now_et = datetime.now(ET)

    bar_start = datetime.combine(prior, dtime(15, 45), ET)
    bars_by_symbol = fetch_bars(symbols, bar_start, now_et)

    earnings = fetch_earnings(session, warnings)
    earnings.update({k: v for k, v in fetch_earnings(prior, warnings).items()
                     if k not in earnings})

    movers = []
    for symbol, bars in bars_by_symbol.items():
        meta = meta_by_ticker.get(symbol)
        if not meta:
            continue
        row = analyse(meta, bars, session, prior)
        if not row or abs(row["gap_pct"]) < MIN_GAP:
            continue

        report = earnings.get(symbol)
        row["earnings"] = {
            "reported": bool(report),
            "timing": classify_timing(report.get("time_raw")) if report else None,
            "date": report.get("date") if report else None,
            "eps_actual": report.get("eps_actual") if report else None,
            "eps_estimate": report.get("eps_estimate") if report else None,
            "surprise_pct": report.get("surprise_pct") if report else None,
        }
        movers.append(row)

    movers = sort_movers(movers)

    covered = len(bars_by_symbol)
    if covered < len(symbols) * 0.5:
        warnings.append(
            f"Only {covered}/{len(symbols)} symbols returned bars -- "
            "IEX coverage may be degraded."
        )

    output = {
        "as_of": now_et.isoformat(),
        "session_date": session.strftime("%Y-%m-%d"),
        "prior_session_date": prior.strftime("%Y-%m-%d"),
        "universe_size": len(symbols),
        "symbols_with_bars": covered,
        "thresholds": {
            "min_market_cap": MIN_MARKET_CAP,
            "min_gap": MIN_GAP,
            "reversal": REVERSAL_THRESHOLD,
        },
        "data_sources": {
            "universe": universe_source,
            "bars": "alpaca_iex",
            "earnings": "nasdaq_calendar",
        },
        "data_caveat": (
            "Bars are Alpaca free-tier IEX feed (~5-10% of consolidated "
            "volume). Prices are representative for large caps; volume is "
            "indicative only."
        ),
        "warnings": warnings,
        "or_window": or_window_label(),
        "windows": build_windows(),
        "movers": movers,
    }

    DATA_DIR.mkdir(exist_ok=True)
    payload = json.dumps(output, indent=1)
    output_path().write_text(payload)
    (DATA_DIR / f"{session:%Y-%m-%d}.json").write_text(payload)

    log.info("Done: %d movers from %d names. Warnings: %d",
             len(movers), len(symbols), len(warnings))
    for mover in movers[:10]:
        log.info("  %-6s %+6.2f%% gap  %+6.2f%% OR  reversal=%-5s earnings=%s",
                 mover["ticker"], mover["gap_pct"] * 100,
                 mover["move_or"] * 100, mover["reversal"],
                 mover["earnings"]["reported"])

    write_status(STATUS_CAPTURED, f"wrote {output_path().as_posix()}",
                 session.strftime("%Y-%m-%d"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
