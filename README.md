# almanac

Provable, plain-text date arithmetic and business-day deadlines. One file, Python 3.9+, standard library only.

You keep a calendar document as plain text: which days are the weekend, any holidays, and the date questions you care about. `almanac` parses it, resolves every date, refuses invalid dates and impossible ranges, counts business days deterministically, draws a self-contained HTML report, and can *seal* the document — recording its hash, a digest of the results, and the counts in a hash-chained log so a deadline calculation cannot be quietly rewritten.

## Quick start

```bash
python almanac.py selftest
python almanac.py check    calendar.almanac
python almanac.py compute  calendar.almanac
python almanac.py render   calendar.almanac --out report.html
python almanac.py seal     calendar.almanac --note "Q2 deadlines"
python almanac.py history
python almanac.py verify
```

## Document grammar

```text
# comment
; comment

title "Q2 deadlines"
weekend sat sun
holiday 2026-03-05 "Team offsite"

add 10 business days to 2026-03-02
add 14 calendar days to 2026-03-02
between 2026-03-02 and 2026-03-16 business days
next friday after 2026-03-02
check 2026-03-06 is business
check 2026-03-07 is nonworking
```

- `title "..."` — optional report title
- `weekend DAY [DAY...]` — non-working weekdays (default `sat sun`)
- `holiday YYYY-MM-DD ["label"]` — extra non-working days
- `add N business|calendar days to YYYY-MM-DD` — add days to a date
- `between YYYY-MM-DD and YYYY-MM-DD business|calendar days` — count days in a range
- `next WEEKDAY after YYYY-MM-DD` — first matching weekday strictly after the date
- `check YYYY-MM-DD is business|nonworking` — assert a day's status

Dates must be strict ISO `YYYY-MM-DD` with zero-padding. Weekdays accept full or three-letter names.

## Validation (fail-closed)

| Rule | Meaning |
|---|---|
| `no-queries` | No date questions declared |
| `duplicate-weekend` | The weekend is declared more than once |
| `duplicate-holiday` | A holiday is declared more than once |
| `bad-date` | A date is not a valid ISO calendar date |
| `bad-weekday` | A weekday name is not recognized |
| `bad-count` | A count is missing, negative, fractional, or too large |
| `date-range` | The end date is before the start date |
| `check-failed` | A `check` does not hold |

All findings are `error` severity. `check`/`compute` exit `1` if any findings
exist, and `seal` refuses to record a document with findings. A syntactically
malformed line exits `2`.

## Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `check` | Clean | Findings | Parse/usage/missing |
| `compute` | Clean results | Findings | Parse/usage/missing |
| `render` | Always | — | Parse/usage/missing |
| `seal` | Sealed | Findings/refused | Parse/usage/missing |
| `history`/`verify` | OK | Chain broken (verify) | Usage/store error |
| `selftest` | All passed | Failures | — |

## Seal ledger

Seals are recorded in a hash-chained JSONL ledger:

- Default: `~/.almanac/ledger.jsonl`
- Override: `ALMANAC_STORE` env var or `--store`
- Deterministic timestamps: pass `--now ISO8601`

Each record links to the previous record's hash (`prev`), includes the document
hash, a canonical digest of the computed results, and counts. `verify` detects
edits, deletions, and reordering.

## Gates (CI)

- `ci/import_gate.py` — stdlib-only imports (local module exempt)
- `ci/check_counts.py` — enforces `selftest >= 92`, `e2e >= 44`
- `ci/prove_suites_can_fail.py` — proves suites fail on negative controls
- `ci/mutation.py` — mutation sweep; requires named check failures, kills real mutants, canary must survive
- 5-cell CI matrix + gate job

## Files

- `almanac.py` — single-file implementation
- `tests/test_almanac.py` — e2e tests (subprocess)
- `ci/*.py` — gates
- `.github/workflows/ci.yml` — CI
- `examples/deadlines.almanac` — worked example
- `REPORT.md` — design notes and verification

## License

MIT (c) 2026 Hema
