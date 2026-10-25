# REPORT: almanac

## Goal
Ship a seventh non-security tool `almanac` (provable, plain-text date arithmetic
and business-day deadlines) to the family bar, published under `happymode-25`.

## Design
- **Single file**: `almanac.py`, Python 3.9+, standard library only.
- **Grammar**: `title "..."`, `weekend DAY [...]`, `holiday YYYY-MM-DD ["label"]`,
  `add N business|calendar days to YYYY-MM-DD`, `between A and B business|calendar days`,
  `next WEEKDAY after YYYY-MM-DD`, `check D is business|nonworking`. Comments `#`/`;`.
- **Date strictness**: ISO `YYYY-MM-DD` with zero-padding; invalid calendar
  dates and impossible months/days are refused (`bad-date`). Leap days handled
  by `date.fromisoformat`.
- **Business days**: working days skip the declared weekend (default `sat sun`)
  and declared holidays. `add N business days` advances forward, counting only
  working days; `N == 0` returns the original date. `between` counts working
  days in `[start, end)` for business mode.
- **Validation (fail-closed)**: `no-queries`, `duplicate-weekend`,
  `duplicate-holiday`, `bad-date`, `bad-weekday`, `bad-count`, `date-range`,
  `check-failed`.
- **Rendering**: self-contained HTML table of results, no external URLs/scripts.
- **Seal ledger**: hash-chained JSONL at `~/.almanac/ledger.jsonl` (env
  `ALMANAC_STORE`, `--store`, `--now`). Records document hash, canonical digest
  of results, counts. `verify` checks seq/prev/hash.
- **Exit codes**: `0` clean/sealed; `1` findings/refused; `2` parse/usage/missing.
  `render` always exits `0`.

## Verification
- **Selftest**: 92 checks — all pass.
- **E2E tests**: 45 tests (`tests/test_almanac.py`) — all pass.
- **Gates**: `import_gate` OK; `check_counts` OK (92/45); `prove_suites_can_fail`
  OK; `mutation` 13/14 real mutants killed with named failures, canary survived.
- **CLI smoke**: `check`/`compute`/`render`/`seal`/`history`/`verify`/`selftest` OK.

## Notes
- Distinct from the family: measurement calibration, arguments, money, votes,
  scheduling, unit conversion, and calendar/business-day arithmetic.
- Strict date parsing rejects unpadded forms and non-ISO formats.
- Business-day arithmetic is explicitly holiday-aware and configurable.
