# gap-scanner-v2

US large-cap gap scanner. One capture per trading day, one output file, one
workflow.

## What it does

At **09:45:30 ET** every trading day, `scan.py` screens the US large-cap
universe (market cap >= $10B, Nasdaq screener) for names that gapped at least
**5%** from the prior session's close to the 09:30 open, and writes them to
`data/latest.json` sorted by `abs(gap_pct)` descending.

Capturing at 09:45:30 gives the gap plus a complete 15-minute opening range,
from bars that have had 15 minutes to print.

```
https://raw.githubusercontent.com/<owner>/gap-scanner-v2/main/data/latest.json
```

## Output

Top level:

| field | meaning |
| --- | --- |
| `as_of` | when the capture ran (ET, ISO 8601) |
| `session_date` / `prior_session_date` | the sessions being compared |
| `or_window` | `09:30-09:45 ET` |
| `windows` | human-readable label per measured window, derived from the code's own time constants |
| `thresholds` | `min_market_cap`, `min_gap`, `reversal` |
| `warnings` | degradations that did not stop the run (stale universe cache, earnings calendar down) |
| `movers` | the result; an empty list is a valid result, not an error |

Per mover:

| field | meaning |
| --- | --- |
| `gap_pct` | `open_930 / prior_close - 1` |
| `move_or` | `or_close / open_930 - 1` — the full 09:30-09:45 move |
| `or_range_pct`, `or_high`, `or_low` | the 09:30-09:45 range |
| `premkt_last`, `premkt_move`, `premkt_bar_count` | 04:00-09:30 |
| `reversal` | `move_or` opposes `gap_pct` by at least 2% — a gap that failed through its opening range, in either direction |
| `earnings` | Nasdaq calendar join: reported, BMO/AMC, EPS actual vs estimate |

Bars are Alpaca's free-tier IEX feed (~5-10% of consolidated volume). Prices
are representative for large caps; volume is indicative only.

## How it runs

`.github/workflows/scan.yml`, one job, two crons:

```
23 13 * * 1-5    09:23 ET during EDT
23 14 * * 1-5    09:23 ET during EST
```

Both fire year-round. The job starts at ~09:23 ET and **sleeps** to 09:45:30
rather than trying to be started at the right moment, because Actions can
delay a job's start but not what it does once running. Whichever cron is the
wrong season for today exits on the guard window (EST morning, 08:23 ET) or on
the same-day idempotency check (EDT morning, 10:23 ET, today's file already
committed).

There are no backup crons, no watchdog, and no retry. **If a day is missed, it
is missed.**

## The freshness assertion

The previous scanner produced 142 green runs while committing nothing, because
nothing ever checked. The last step of the job is `verify.py`, which reads the
*committed* `data/latest.json` and exits non-zero unless `session_date` is
today in `America/New_York` and `as_of` is within 30 minutes of now.

It has to tell two things apart:

- **deliberate no-op → green.** `scan.py` records its exit reason to
  `$SCAN_STATUS_FILE`: `weekend`, `market_holiday`, `outside_guard_window`, or
  `already_captured_today`. There was nothing to capture, so there is nothing
  to assert.
- **silent no-op → red.** `scan.py` claimed a capture and the committed file
  is not today's, or is stale — or no status was recorded at all, in which
  case we cannot tell which case we are in, and "cannot tell" is precisely the
  failure this step exists to end.

It fails the run. It does not retry, heal, dispatch, or notify.

## Local

```
pip install -r requirements.txt
python -m unittest discover -v          # tests, no network
FORCE_RUN=1 python scan.py              # capture now, bypassing guard and sleep
```

`FORCE_RUN=1` needs `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY` in the
environment. `.env` is gitignored.

## Configuration

| env var | default |
| --- | --- |
| `MIN_GAP` | `0.05` |
| `MIN_MARKET_CAP` | `10e9` |
| `FORCE_RUN` | unset |
| `SCAN_STATUS_FILE` | unset (no status written) |

Repo secrets: `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`.
Settings → Actions → General → Workflow permissions must be **Read and write**.
