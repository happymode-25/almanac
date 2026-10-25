#!/usr/bin/env python3
"""almanac -- provable, plain-text date arithmetic and business-day deadlines.

You keep a calendar document as plain text: which days are the weekend, any
holidays, and the date questions you care about. almanac parses it, resolves
every date, refuses invalid dates and impossible ranges, counts business days
deterministically, draws a self-contained report, and can *seal* the document --
recording its hash, a digest of the results, and the counts in a hash-chained
log so a deadline calculation cannot be quietly rewritten.

Business-day counts skip the declared weekend and the declared holidays. There
is no locale guessing and no DST arithmetic: dates are ISO calendar dates, and
the rules are the ones you wrote down.

Single file, Python 3.9+, standard library only.

    python almanac.py selftest
    python almanac.py check   calendar.almanac
    python almanac.py compute calendar.almanac
    python almanac.py render  calendar.almanac --out report.html
    python almanac.py seal    calendar.almanac --note "Q2 deadlines"
    python almanac.py verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import tempfile
from datetime import date, timedelta

TOOL = "almanac"
VERSION = "1.0.0"
GENESIS = "0" * 64

RULES = ("no-queries", "duplicate-weekend", "duplicate-holiday", "bad-date",
         "bad-weekday", "bad-count", "date-range", "check-failed")

# How far a single business-day count may run before we refuse rather than spin.
MAX_BUSINESS = 1_000_000

WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ADD_RE = re.compile(r"^add\s+(\S+)\s+(business|calendar)\s+days?\s+to\s+(\S+)$",
                    re.IGNORECASE)
BETWEEN_RE = re.compile(
    r"^between\s+(\S+)\s+and\s+(\S+)\s+(business|calendar)\s+days?$",
    re.IGNORECASE)
NEXT_RE = re.compile(r"^next\s+(\S+)\s+after\s+(\S+)$", re.IGNORECASE)
CHECK_RE = re.compile(r"^check\s+(\S+)\s+is\s+(business|nonworking)$",
                      re.IGNORECASE)
HOLIDAY_RE = re.compile(r"^holiday\s+(\S+)(?:\s+(.*))?$", re.IGNORECASE)
WEEKEND_RE = re.compile(r"^weekend\s+(.+)$", re.IGNORECASE)


class AlmanacError(Exception):
    """A refusal: bad syntax, bad store, bad record. Always exits non-zero."""


class DateProblem(Exception):
    """A semantic date problem, reported as a finding rather than a refusal."""


class BadDate(DateProblem):
    pass


class BadWeekday(DateProblem):
    def __init__(self, name):
        super().__init__("unknown weekday %r" % name)
        self.name = name


class BadCount(DateProblem):
    pass


def fail(message: str):
    raise AlmanacError(message)


# --------------------------------------------------------------------------
# canonical hashing (the family discipline)
# --------------------------------------------------------------------------

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return sha256_hex(canonical(body))


def now_iso(now=None) -> str:
    if now:
        return now
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# parsing (shape only; dates and weekdays are resolved during validation)
# --------------------------------------------------------------------------

def parse_document(text: str) -> dict:
    if text.startswith("\ufeff"):
        text = text[1:]
    title = None
    weekends = []
    holidays = []
    queries = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        low = s.lower()

        if low == "title":
            fail("line %d: title needs a value" % lineno)
        if low.startswith("title "):
            title = s[len("title "):].strip().strip('"')
            continue

        if low == "weekend":
            fail("line %d: weekend needs one or more weekdays" % lineno)
        if low.startswith("weekend "):
            names = [n for n in re.split(r"[,\s]+", s[len("weekend "):].strip())
                     if n]
            if not names:
                fail("line %d: weekend needs one or more weekdays" % lineno)
            weekends.append({"names": names, "line": lineno})
            continue

        if low == "holiday":
            fail("line %d: holiday needs a date" % lineno)
        if low.startswith("holiday "):
            match = HOLIDAY_RE.match(s)
            if not match:
                fail("line %d: holiday needs a date" % lineno)
            label = (match.group(2) or "").strip().strip('"')
            holidays.append({"date": match.group(1), "label": label,
                             "line": lineno})
            continue

        if low == "add":
            fail("line %d: add needs 'N business|calendar days to DATE'" % lineno)
        if low.startswith("add "):
            match = ADD_RE.match(s)
            if not match:
                fail("line %d: add needs 'N business|calendar days to DATE'"
                     % lineno)
            queries.append({"kind": "add", "line": lineno, "text": s,
                            "count": match.group(1),
                            "basis": match.group(2).lower(),
                            "date": match.group(3)})
            continue

        if low == "between":
            fail("line %d: between needs 'DATE and DATE business|calendar days'"
                 % lineno)
        if low.startswith("between "):
            match = BETWEEN_RE.match(s)
            if not match:
                fail("line %d: between needs 'DATE and DATE "
                     "business|calendar days'" % lineno)
            queries.append({"kind": "between", "line": lineno, "text": s,
                            "start": match.group(1), "end": match.group(2),
                            "basis": match.group(3).lower()})
            continue

        if low == "next":
            fail("line %d: next needs 'WEEKDAY after DATE'" % lineno)
        if low.startswith("next "):
            match = NEXT_RE.match(s)
            if not match:
                fail("line %d: next needs 'WEEKDAY after DATE'" % lineno)
            queries.append({"kind": "next", "line": lineno, "text": s,
                            "weekday": match.group(1), "date": match.group(2)})
            continue

        if low == "check":
            fail("line %d: check needs 'DATE is business|nonworking'" % lineno)
        if low.startswith("check "):
            match = CHECK_RE.match(s)
            if not match:
                fail("line %d: check needs 'DATE is business|nonworking'"
                     % lineno)
            queries.append({"kind": "check", "line": lineno, "text": s,
                            "date": match.group(1),
                            "expect": match.group(2).lower()})
            continue

        fail("line %d: not a directive: %r" % (lineno, s))

    return {"title": title, "weekends": weekends, "holidays": holidays,
            "queries": queries}


# --------------------------------------------------------------------------
# dates and weekdays
# --------------------------------------------------------------------------

def parse_date(text: str) -> date:
    s = text.strip()
    if not DATE_RE.match(s):
        raise BadDate("not an ISO date: %r" % text)
        raise BadDate("not an ISO date: %r" % text)
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s:
            raise BadDate("not a real date: %r" % text)
        return d
    except ValueError:
        raise BadDate("not a real date: %r" % text)


def parse_weekday(text: str) -> int:
    key = text.strip().lower()
    if key not in WEEKDAYS:
        raise BadWeekday(text)
    return WEEKDAYS[key]


def parse_count(text: str) -> int:
    s = text.strip()
    try:
        value = int(s)
    except ValueError:
        raise BadCount("not a whole number: %r" % text)
    if value < 0:
        raise BadCount("a day count cannot be negative: %r" % text)
    if value > MAX_BUSINESS:
        raise BadCount("a day count is too large: %r" % text)
    return value


def weekday_name(index: int) -> str:
    return WEEKDAY_NAMES[index]


def is_working(day: date, weekend: set, holidays: set) -> bool:
    return day.weekday() not in weekend and day not in holidays


def add_business(start: date, count: int, weekend: set, holidays: set) -> date:
    current = start
    remaining = count
    while remaining > 0:
        current += timedelta(days=1)
        if is_working(current, weekend, holidays):
            remaining -= 1
    return current


def add_calendar(start: date, count: int) -> date:
    try:
        return start + timedelta(days=count)
    except OverflowError:
        raise BadCount("the result falls outside the calendar")


def count_business(start: date, end: date, weekend: set, holidays: set) -> int:
    total = 0
    current = start
    while current < end:
        if is_working(current, weekend, holidays):
            total += 1
        current += timedelta(days=1)
    return total


def next_weekday(start: date, target: int) -> date:
    current = start
    for _ in range(7):
        current += timedelta(days=1)
        if current.weekday() == target:
            return current
    return current
    raise BadWeekday("could not find the next weekday")


# --------------------------------------------------------------------------
# validation and computation
# --------------------------------------------------------------------------

def F(rule: str, line: int, message: str) -> dict:
    return {"rule": rule, "severity": "error", "line": line, "message": message}


def resolve(document: dict):
    findings = []
    results = {"adds": [], "spans": [], "nexts": [], "checks": []}

    if not document["queries"]:
        findings.append(F("no-queries", 0,
                          "no date questions are declared"))

    if len(document["weekends"]) > 1:
        findings.append(F("duplicate-weekend",
                          document["weekends"][1]["line"],
                          "the weekend is declared more than once"))

    weekend = {5, 6}
    if document["weekends"]:
        chosen = document["weekends"][0]
        days = set()
        for name in chosen["names"]:
            try:
                days.add(parse_weekday(name))
            except BadWeekday as exc:
                findings.append(F("bad-weekday", chosen["line"], str(exc)))
        if days:
            weekend = days

    holidays = set()
    for entry in document["holidays"]:
        try:
            day = parse_date(entry["date"])
        except BadDate as exc:
            findings.append(F("bad-date", entry["line"], str(exc)))
            continue
        if day in holidays:
            findings.append(F("duplicate-holiday", entry["line"],
                              "%s is declared more than once"
                              % day.isoformat()))
            continue
        holidays.add(day)

    for query in document["queries"]:
        line = query["line"]
        kind = query["kind"]

        if kind == "add":
            try:
                count = parse_count(query["count"])
            except BadCount as exc:
                findings.append(F("bad-count", line, str(exc)))
                continue
            try:
                start = parse_date(query["date"])
            except BadDate as exc:
                findings.append(F("bad-date", line, str(exc)))
                continue
            try:
                if query["basis"] == "business":
                    result = add_business(start, count, weekend, holidays)
                else:
                    result = add_calendar(start, count)
            except BadCount as exc:
                findings.append(F("bad-count", line, str(exc)))
                continue
            results["adds"].append({"line": line, "text": query["text"],
                                    "result": result.isoformat(),
                                    "basis": query["basis"]})

        elif kind == "between":
            try:
                start = parse_date(query["start"])
                end = parse_date(query["end"])
            except BadDate as exc:
                findings.append(F("bad-date", line, str(exc)))
                continue
            if end < start:
                findings.append(F("date-range", line,
                                  "the end %s is before the start %s"
                                  % (end.isoformat(), start.isoformat())))
                continue
            if query["basis"] == "business":
                total = count_business(start, end, weekend, holidays)
            else:
                total = (end - start).days
            results["spans"].append({"line": line, "text": query["text"],
                                     "days": total,
                                     "basis": query["basis"]})

        elif kind == "next":
            try:
                start = parse_date(query["date"])
            except BadDate as exc:
                findings.append(F("bad-date", line, str(exc)))
                continue
            try:
                target = parse_weekday(query["weekday"])
            except BadWeekday as exc:
                findings.append(F("bad-weekday", line, str(exc)))
                continue
            result = next_weekday(start, target)
            results["nexts"].append({"line": line, "text": query["text"],
                                     "result": result.isoformat()})

        else:
            try:
                day = parse_date(query["date"])
            except BadDate as exc:
                findings.append(F("bad-date", line, str(exc)))
                continue
            working = is_working(day, weekend, holidays)
            ok = working if query["expect"] == "business" else not working
            results["checks"].append({"line": line, "text": query["text"],
                                      "ok": ok,
                                      "working": working})
            if not ok:
                findings.append(F("check-failed", line,
                                  "%s is %s, not %s"
                                  % (day.isoformat(),
                                     "business" if working else "nonworking",
                                     query["expect"])))

    findings.sort(key=lambda finding: (finding["line"], finding["rule"]))
    return findings, results


def summarize(document: dict, results: dict) -> dict:
    return {
        "holidays": len(document["holidays"]),
        "adds": len(results["adds"]),
        "spans": len(results["spans"]),
        "nexts": len(results["nexts"]),
        "checks": len(results["checks"]),
    }


# --------------------------------------------------------------------------
# the seal ledger (hash-chained)
# --------------------------------------------------------------------------

def resolve_store(explicit):
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("ALMANAC_STORE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".almanac" / "ledger.jsonl"


def load_records(store: pathlib.Path) -> list:
    if not store.exists():
        return []
    records = []
    with open(store, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                fail("store %s: line %d is not valid JSON" % (store, lineno))
    return records


def append_record(store: pathlib.Path, rec: dict) -> dict:
    records = load_records(store)
    prev = records[-1]["hash"] if records else GENESIS
    rec = dict(rec)
    rec["seq"] = len(records)
    rec["prev"] = prev
    rec["hash"] = record_hash(rec)
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "a", encoding="utf-8") as fh:
        fh.write(canonical(rec) + "\n")
    return rec


def verify_chain(records: list) -> list:
    problems = []
    expected_prev = GENESIS
    for i, rec in enumerate(records):
        if rec.get("seq") != i:
            problems.append("record %d: seq is %r, expected %d"
                            % (i, rec.get("seq"), i))
        if rec.get("prev") != expected_prev:
            problems.append("record %d: prev link is broken" % i)
        if rec.get("hash") != record_hash(rec):
            problems.append("record %d: content hash mismatch" % i)
        expected_prev = rec.get("hash")
    return problems


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(name: str, document: dict, findings: list, results,
                title: str) -> str:
    summary = summarize(document, results) if results is not None else None

    if summary:
        cards = "".join(
            "<div class='card'><div class='n'>%s</div><div class='l'>%s</div></div>"
            % (_esc(value), label)
            for value, label in [
                (summary["adds"], "adds"),
                (summary["spans"], "spans"),
                (summary["nexts"], "nexts"),
                (summary["checks"], "checks")])
    else:
        cards = ("<div class='card'><div class='n'>%d</div><div class='l'>errors</div></div>"
                 % len(findings))

    rows = []
    if results is not None:
        for item in results["adds"]:
            rows.append("<tr><td>add</td><td>%s</td><td>%s</td></tr>"
                        % (_esc(item["text"]), _esc(item["result"])))
        for item in results["spans"]:
            rows.append("<tr><td>span</td><td>%s</td><td>%d %s days</td></tr>"
                        % (_esc(item["text"]), item["days"],
                           _esc(item["basis"])))
        for item in results["nexts"]:
            rows.append("<tr><td>next</td><td>%s</td><td>%s</td></tr>"
                        % (_esc(item["text"]), _esc(item["result"])))
        for item in results["checks"]:
            verdict = ("<span class='ok'>holds</span>" if item["ok"]
                       else "<span class='sev'>fails</span>")
            rows.append("<tr><td>check</td><td>%s</td><td>%s</td></tr>"
                        % (_esc(item["text"]), verdict))
    table_html = ("<table><thead><tr><th>kind</th><th>declaration</th>"
                  "<th>result</th></tr></thead><tbody>%s</tbody></table>"
                  % "".join(rows)) if rows else ""

    if not document["queries"]:
        verdict = "<p class='empty'>No dates to resolve.</p>"
    elif results is not None:
        verdict = ("<p class='ok'>%d dates resolved, %d checks evaluated.</p>"
                   % (len(results["adds"]) + len(results["spans"])
                      + len(results["nexts"]), len(results["checks"])))
    else:
        verdict = ""

    if findings:
        items_html = "".join(
            "<li><span class='sev'>%s</span> line %d: %s</li>"
            % (_esc(f["rule"]), f["line"], _esc(f["message"])) for f in findings)
        finding_html = "<h2>Findings</h2><ul class='findings'>%s</ul>" % items_html
    else:
        finding_html = ("<p class='ok'>Every date is real and every count "
                        "resolved.</p>")

    holiday_rows = "".join(
        "<tr><td>%s</td><td>%s</td></tr>"
        % (_esc(h["date"]), _esc(h["label"])) for h in document["holidays"])
    holiday_table = ("<h2>Holidays</h2><table><thead><tr><th>date</th>"
                     "<th>label</th></tr></thead><tbody>%s</tbody></table>"
                     % holiday_rows) if document["holidays"] else ""

    weekend_text = "sat, sun"
    if document["weekends"]:
        weekend_text = ", ".join(document["weekends"][0]["names"])

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
:root { color-scheme: dark; }
body { margin:0; background:#1a1b26; color:#c0caf5;
  font-family:system-ui,-apple-system,Segoe UI,sans-serif; }
header { padding:28px 32px 8px; }
h1 { margin:0 0 6px; font-size:22px; }
.sub { color:#565f89; font-size:14px; }
main { padding:16px 32px 40px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; }
.card { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; min-width:110px; }
.card .n { font-size:20px; font-weight:600; }
.card .l { color:#565f89; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
h2 { font-size:15px; color:#565f89; text-transform:uppercase; letter-spacing:.05em; margin:26px 0 8px; }
table { border-collapse:collapse; width:100%%; max-width:820px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid #292e42; font-size:14px; }
th { color:#565f89; font-weight:600; font-size:12px; text-transform:uppercase; }
.findings { list-style:none; padding:0; }
.findings li { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; margin:8px 0; font-size:14px; }
.sev { color:#f7768e; font-weight:700; font-size:11px; text-transform:uppercase; margin-right:8px; }
.ok { color:#9ece6a; }
.empty { color:#565f89; }
</style></head><body>
<header>
  <h1>%s</h1>
  <div class="sub">%d questions &middot; weekend %s</div>
</header>
<main>
  <div class="cards">%s</div>
  %s
  <h2>Results</h2>
  %s
  %s
  %s
</main></body></html>
""" % (_esc(title), _esc(title), len(document["queries"]),
       _esc(weekend_text), cards, verdict, table_html,
       holiday_table if holiday_table else "", finding_html)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def read_file(path: str):
    if path == "-":
        data = sys.stdin.buffer.read()
        return data.decode("utf-8"), "<stdin>", sha256_bytes(data)
    p = pathlib.Path(path)
    if not p.exists():
        fail("no such file: %s" % path)
    data = p.read_bytes()
    return data.decode("utf-8"), p.name, sha256_bytes(data)


def report_text(name, document, findings, results) -> str:
    lines = ["document: %s" % name,
             "%d questions, %d holidays, %d weekend rule%s"
             % (len(document["queries"]), len(document["holidays"]),
                len(document["weekends"]),
                "" if len(document["weekends"]) == 1 else "s"),
             ""]
    if results is not None:
        for item in results["adds"]:
            lines.append("  add      %s = %s"
                         % (item["text"], item["result"]))
        for item in results["spans"]:
            lines.append("  span     %s = %d %s days"
                         % (item["text"], item["days"], item["basis"]))
        for item in results["nexts"]:
            lines.append("  next     %s = %s"
                         % (item["text"], item["result"]))
        for item in results["checks"]:
            lines.append("  check    %s  %s"
                         % (item["text"],
                            "holds" if item["ok"] else "FAILS"))
    if findings:
        lines.append("")
        lines.append("%d findings:" % len(findings))
        for finding in findings:
            lines.append("  [%s] line %d: %s"
                         % (finding["rule"], finding["line"],
                            finding["message"]))
    else:
        lines.append("")
        lines.append("no findings")
    return "\n".join(lines)


def cmd_check(path, as_json):
    text, name, _ = read_file(path)
    document = parse_document(text)
    findings, _ = resolve(document)
    if as_json:
        print(json.dumps({
            "document": name,
            "findings": findings,
            "summary": {"queries": len(document["queries"]),
                        "holidays": len(document["holidays"])},
        }, indent=2, sort_keys=True))
    else:
        print(report_text(name, document, findings, None))
    return 1 if findings else 0


def cmd_compute(path, as_json):
    text, name, _ = read_file(path)
    document = parse_document(text)
    findings, results = resolve(document)
    if findings:
        if as_json:
            print(json.dumps({"document": name, "findings": findings,
                              "results": results}, indent=2, sort_keys=True))
        else:
            print(report_text(name, document, findings, None))
        return 1
    if as_json:
        print(json.dumps({
            "document": name,
            "holidays": len(document["holidays"]),
            "adds": results["adds"],
            "spans": results["spans"],
            "nexts": results["nexts"],
            "checks": results["checks"],
            "summary": summarize(document, results),
        }, indent=2, sort_keys=True))
    else:
        print(report_text(name, document, findings, results))
    return 0


def cmd_render(path, out, title):
    text, name, _ = read_file(path)
    document = parse_document(text)
    findings, results = resolve(document)
    html = render_html(name, document, findings, results, title or name)
    out_path = pathlib.Path(out)
    if out_path.parent and str(out_path.parent) not in ("", "."):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print("wrote report to %s" % out)
    return 0


def cmd_seal(path, note, store, now):
    text, name, document_hash = read_file(path)
    document = parse_document(text)
    findings, results = resolve(document)
    if findings:
        count = len(findings)
        print("refusing to seal: the document has %d error%s"
              % (count, "" if count == 1 else "s"))
        for finding in findings:
            print("  [%s] line %d: %s"
                  % (finding["rule"], finding["line"], finding["message"]))
        return 1
    summary = summarize(document, results)
    results_hash = sha256_hex(canonical(results))
    rec = append_record(store, {
        "ts": now_iso(now), "tool": TOOL, "version": VERSION, "kind": "seal",
        "document": name, "document_hash": document_hash,
        "results_hash": results_hash,
        "holidays": summary["holidays"],
        "adds": summary["adds"],
        "spans": summary["spans"],
        "nexts": summary["nexts"],
        "checks": summary["checks"],
        "note": note or "",
    })
    print("sealed %s" % name)
    print("  document hash %s" % document_hash[:12])
    print("  results hash  %s" % results_hash[:12])
    print("  add/spans     %d/%d" % (summary["adds"], summary["spans"]))
    print("  nexts/checks  %d/%d" % (summary["nexts"], summary["checks"]))
    print("  recorded as seq %d" % rec["seq"])
    return 0


def cmd_history(as_json, store):
    records = load_records(store)
    if as_json:
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0
    if not records:
        print("no seals recorded")
        return 0
    for rec in records:
        print("%4d  %s  %-18s  %d adds  %d spans  %d checks  %s"
              % (rec["seq"], rec["ts"], rec["document"],
                 rec.get("adds", 0), rec.get("spans", 0),
                 rec.get("checks", 0), rec.get("note", "")))
    return 0


def cmd_verify(as_json, store):
    records = load_records(store)
    problems = verify_chain(records)
    if as_json:
        print(json.dumps({"ok": not problems, "records": len(records),
                          "problems": problems}, indent=2, sort_keys=True))
        return 0 if not problems else 1
    if not problems:
        print("chain intact: %d seals" % len(records))
        return 0
    print("chain broken: %d problems" % len(problems))
    for problem in problems:
        print("  - %s" % problem)
    return 1


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.total = 0
        self.failed = []

    def ok(self, condition, name):
        self.total += 1
        if not condition:
            self.failed.append(name)

    def eq(self, got, want, name):
        self.total += 1
        if got != want:
            self.failed.append("%s (got %r, want %r)" % (name, got, want))

    def report(self):
        if not self.failed:
            print("all checks passed: %d" % self.total)
            return 0
        print("FAIL: %d of %d checks failed" % (len(self.failed), self.total))
        for name in self.failed:
            print("  - %s" % name)
        return 1


def _p(*lines) -> str:
    return "\n".join(lines) + "\n"


CLEAN = _p(
    'title "Q2 deadlines"',
    "weekend sat sun",
    'holiday 2026-03-05 "Team offsite"',
    "add 10 business days to 2026-03-02",
    "add 14 calendar days to 2026-03-02",
    "add 0 business days to 2026-03-02",
    "between 2026-03-02 and 2026-03-16 business days",
    "between 2026-03-02 and 2026-03-16 calendar days",
    "next friday after 2026-03-02",
    "check 2026-03-06 is business",
    "check 2026-03-07 is nonworking",
)


def cmd_selftest():
    checks = Checks()

    def rules_of(text):
        try:
            document = parse_document(text)
        except AlmanacError:
            return None
        return [f["rule"] for f in resolve(document)[0]]

    # --- parsing ----------------------------------------------------------
    doc = parse_document(_p('title "Site"', "weekend fri, sat",
                            'holiday 2026-01-01 "New Year"',
                            "add 5 business days to 2026-03-02",
                            "between 2026-03-02 and 2026-04-01 business days",
                            "next friday after 2026-03-02",
                            "check 2026-03-07 is nonworking"))
    checks.eq(doc["title"], "Site", "a title directive is unquoted")
    checks.eq(doc["weekends"][0]["names"], ["fri", "sat"],
              "a weekend list parses")
    checks.eq(doc["holidays"][0]["date"], "2026-01-01",
              "a holiday date parses")
    checks.eq(doc["holidays"][0]["label"], "New Year",
              "a holiday label is unquoted")
    checks.eq(doc["queries"][0]["kind"], "add", "an add line parses")
    checks.eq(doc["queries"][0]["count"], "5", "an add count parses")
    checks.eq(doc["queries"][0]["basis"], "business", "an add basis parses")
    checks.eq(doc["queries"][0]["date"], "2026-03-02",
              "an add date parses")
    checks.eq(doc["queries"][1]["kind"], "between", "a between line parses")
    checks.eq(doc["queries"][1]["start"], "2026-03-02",
              "a between start parses")
    checks.eq(doc["queries"][1]["end"], "2026-04-01",
              "a between end parses")
    checks.eq(doc["queries"][2]["kind"], "next", "a next line parses")
    checks.eq(doc["queries"][2]["weekday"], "friday",
              "a next weekday parses")
    checks.eq(doc["queries"][3]["kind"], "check", "a check line parses")
    checks.eq(doc["queries"][3]["expect"], "nonworking",
              "a check expectation parses")
    checks.eq(doc["queries"][0]["line"], 4, "a query remembers its line")
    checks.eq(parse_document("; c\n# c\n\n")["queries"], [],
              "comment and blank lines are skipped")
    checks.eq(parse_document("\ufefftitle \"x\"")["title"], "x",
              "a leading byte-order mark is tolerated")
    checks.eq(parse_document(_p("add 0 calendar days to 2028-02-28"))
              ["queries"][0]["basis"], "calendar",
              "a singular 'day' is accepted")

    for bad, name in [
        (_p("add 5 business days"), "an add without a date is refused"),
        (_p("add 5 business days 2026-03-02"),
         "an add missing the 'to' keyword is refused"),
        (_p("between 2026-03-02 business days"),
         "a between without an end is refused"),
        (_p("next 2026-03-02"), "a next without a weekday is refused"),
        (_p("check 2026-03-02"), "a check without an expectation is refused"),
        (_p("weekend"), "a weekend with no days is refused"),
        (_p("holiday"), "a holiday with no date is refused"),
        (_p("title"), "a title with no value is refused"),
        (_p("frobnicate x"), "an unknown line is refused"),
    ]:
        try:
            parse_document(bad)
            checks.ok(False, name)
        except AlmanacError:
            checks.ok(True, name)

    # --- dates and weekdays ----------------------------------------------
    checks.eq(parse_date("2026-03-02").isoformat(), "2026-03-02",
              "an ISO date parses")
    checks.eq(parse_date("2028-02-29").isoformat(), "2028-02-29",
              "a leap day parses")
    checks.eq(parse_date("2026-03-02").weekday(), 0,
              "2026-03-02 is a Monday")
    checks.eq(parse_weekday("Mon"), 0, "a short weekday parses")
    checks.eq(parse_weekday("SUNDAY"), 6, "a long weekday is case-insensitive")
    checks.eq(weekday_name(5), "saturday", "a weekday index renders")
    for bad_date, name in [("2026-02-30", "an impossible date is refused"),
                           ("2026-3-2", "an unpadded date is refused"),
                           ("02/03/2026", "a non-ISO date is refused"),
                           ("2026-13-01", "a thirteenth month is refused")]:
        try:
            parse_date(bad_date)
            checks.ok(False, name)
        except BadDate:
            checks.ok(True, name)
    for bad_day, name in [("funday", "an unknown weekday is refused"),
                          ("mo", "an ambiguous weekday is refused")]:
        try:
            parse_weekday(bad_day)
            checks.ok(False, name)
        except BadWeekday:
            checks.ok(True, name)
    checks.eq(parse_count("0"), 0, "a zero count is allowed")
    checks.eq(parse_count("12"), 12, "a positive count parses")
    for bad_count, name in [("-1", "a negative count is refused"),
                            ("1.5", "a fractional count is refused"),
                            ("lots", "a word count is refused")]:
        try:
            parse_count(bad_count)
            checks.ok(False, name)
        except BadCount:
            checks.ok(True, name)

    # --- working days -----------------------------------------------------
    weekend, holidays = {5, 6}, set()
    checks.ok(is_working(date(2026, 3, 6), weekend, holidays),
              "a Friday is a business day")
    checks.ok(not is_working(date(2026, 3, 7), weekend, holidays),
              "a Saturday is not a business day")
    checks.eq(add_business(date(2026, 3, 2), 10, weekend, holidays).isoformat(),
              "2026-03-16", "ten business days from a Monday")
    checks.eq(add_business(date(2026, 3, 2), 0, weekend, holidays).isoformat(),
              "2026-03-02", "adding zero business days keeps the date")
    checks.eq(add_calendar(date(2026, 3, 2), 14).isoformat(),
              "2026-03-16", "fourteen calendar days from a Monday")
    checks.eq(add_calendar(date(2028, 2, 28), 1).isoformat(),
              "2028-02-29", "calendar addition crosses a leap day")
    checks.eq(add_calendar(date(2028, 2, 28), 2).isoformat(),
              "2028-03-01", "calendar addition crosses a month end")
    checks.eq(next_weekday(date(2026, 3, 2), 4).isoformat(), "2026-03-06",
              "the next Friday after a Monday")
    checks.eq(next_weekday(date(2026, 3, 6), 4).isoformat(), "2026-03-13",
              "the next Friday is strictly after")
    checks.eq(count_business(date(2026, 3, 2), date(2026, 3, 16),
                             weekend, holidays), 10,
              "ten business days in a fortnight")
    checks.eq(count_business(date(2026, 3, 2), date(2026, 3, 2),
                             weekend, holidays), 0,
              "an empty span has no business days")

    # --- validation -------------------------------------------------------
    checks.eq(resolve(parse_document(CLEAN))[0], [],
              "a clean document has no findings")
    checks.ok("no-queries" in (rules_of("; nothing\n") or []),
              "a document with no questions is reported")
    checks.ok("bad-date" in (rules_of(_p("add 5 business days to 2026-02-30"))
                             or []), "an invalid date is reported")
    checks.ok("bad-count" in (rules_of(_p("add -1 business days to 2026-03-02"))
                              or []), "a negative count is reported")
    checks.ok("bad-weekday" in (rules_of(_p("next funday after 2026-03-02"))
                                or []), "an unknown weekday is reported")
    checks.ok("bad-weekday" in (rules_of(_p("weekend sat funday",
                                            "add 1 business days to 2026-03-02"))
                                or []), "an unknown weekend day is reported")
    checks.ok("date-range" in (rules_of(
        _p("between 2026-04-01 and 2026-03-02 business days")) or []),
        "a reversed span is reported")
    checks.ok("check-failed" in (rules_of(
        _p("check 2026-03-07 is business")) or []),
        "a failing check is reported")
    checks.ok("duplicate-holiday" in (rules_of(
        _p("holiday 2026-01-01", "holiday 2026-01-01",
           "add 1 business days to 2026-01-02")) or []),
        "a duplicate holiday is reported")
    checks.ok("duplicate-weekend" in (rules_of(
        _p("weekend sat sun", "weekend fri sat",
           "add 1 business days to 2026-03-02")) or []),
        "a duplicate weekend is reported")
    both = rules_of(_p("add 5 business days to 2026-02-30",
                       "between 2026-04-01 and 2026-03-02 business days")) or []
    checks.ok("bad-date" in both and "date-range" in both,
              "several findings are all reported")

    # --- computation ------------------------------------------------------
    findings, results = resolve(parse_document(CLEAN))
    checks.eq(findings, [], "the worked document resolves cleanly")
    adds = {a["text"]: a["result"] for a in results["adds"]}
    spans = {s["text"]: s["days"] for s in results["spans"]}
    nexts = {n["text"]: n["result"] for n in results["nexts"]}

    checks.eq(adds["add 10 business days to 2026-03-02"], "2026-03-17",
              "a holiday delays a business-day count")
    checks.eq(adds["add 14 calendar days to 2026-03-02"], "2026-03-16",
              "calendar days ignore holidays")
    checks.eq(adds["add 0 business days to 2026-03-02"], "2026-03-02",
              "zero business days keeps the date")
    checks.eq(spans["between 2026-03-02 and 2026-03-16 business days"], 9,
              "a holiday is excluded from a business span")
    checks.eq(spans["between 2026-03-02 and 2026-03-16 calendar days"], 14,
              "a calendar span counts every day")
    checks.eq(nexts["next friday after 2026-03-02"], "2026-03-06",
              "the next Friday resolves")
    checks.eq(len(results["checks"]), 2, "every check is reported")
    checks.ok(all(c["ok"] for c in results["checks"]),
              "every check in the worked document holds")

    custom = resolve(parse_document(_p(
        "weekend mon tue",
        "add 1 business days to 2026-03-02"))) [1]
    checks.eq(custom["adds"][0]["result"], "2026-03-04",
              "a custom weekend changes the count")

    leap = resolve(parse_document(_p(
        "add 1 calendar days to 2028-02-28",
        "add 2 calendar days to 2028-02-28")))[1]
    checks.eq([a["result"] for a in leap["adds"]],
              ["2028-02-29", "2028-03-01"],
              "leap-day arithmetic is exact")

    # --- exit codes -------------------------------------------------------
    def safe_code(text):
        try:
            document = parse_document(text)
        except AlmanacError:
            return 2
        return 1 if resolve(document)[0] else 0

    checks.eq(safe_code(CLEAN), 0, "a clean document exits 0")
    checks.eq(safe_code(_p("add 5 business days to 2026-02-30")), 1,
              "a document with a finding exits 1")
    checks.eq(safe_code(_p("add 5 business days")), 2,
              "an unparseable document exits 2")

    # --- chain ------------------------------------------------------------
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="almanac-selftest-"))
    try:
        store = tmp / "ledger.jsonl"
        first = append_record(store, {"ts": "2026-01-01T00:00:00",
                                      "kind": "seal", "document": "a"})
        checks.eq(first["seq"], 0, "the first seal has seq 0")
        checks.eq(first["prev"], GENESIS, "the first seal links to genesis")
        second = append_record(store, {"ts": "2026-01-02T00:00:00",
                                       "kind": "seal", "document": "b"})
        checks.eq(second["prev"], first["hash"],
                  "the second seal links to the first hash")
        checks.eq(verify_chain(load_records(store)), [],
                  "a clean seal chain verifies")
        tampered = load_records(store)
        tampered[0]["document"] = "forged"
        checks.ok(any("hash" in p for p in verify_chain(tampered)),
                  "editing a seal is caught")
        deleted = load_records(store)
        del deleted[0]
        checks.ok(len(verify_chain(deleted)) > 0,
                  "deleting the oldest seal is caught")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # --- render -----------------------------------------------------------
    demo = parse_document(CLEAN)
    findings, results = resolve(demo)
    html = render_html("demo", demo, findings, results, "demo")
    checks.ok("add 10 business days to 2026-03-02" in html,
              "render shows a question")
    checks.ok("2026-03-17" in html, "render shows a computed date")
    checks.ok("http://" not in html and "https://" not in html,
              "render contains no external URLs")
    checks.ok("<script" not in html, "render contains no script")
    empty_html = render_html("empty", parse_document(""), [], None, "empty")
    checks.ok("No dates to resolve" in empty_html,
              "render handles an empty document")

    return checks.report()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def extract_now(argv):
    now = None
    out = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--now":
            if i + 1 >= len(argv):
                fail("--now needs a value")
            now = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--now="):
            now = arg.split("=", 1)[1]
            i += 1
            continue
        out.append(arg)
        i += 1
    return now, out


def build_parser():
    parser = argparse.ArgumentParser(
        prog="almanac",
        description="Plain-text date arithmetic and business-day deadlines.")
    parser.add_argument("--version", action="version",
                        version="%s %s" % (TOOL, VERSION))
    sub = parser.add_subparsers(dest="command")

    def add(name, help_text):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--store", default=None)
        sp.add_argument("--version", action="version",
                        version="%s %s" % (TOOL, VERSION))
        return sp

    sp = add("check", "Validate dates and weekday rules.")
    sp.add_argument("document")
    sp.add_argument("--json", action="store_true")

    sp = add("compute", "Resolve adds, spans, nexts, and checks.")
    sp.add_argument("document")
    sp.add_argument("--json", action="store_true")

    sp = add("render", "Write a self-contained HTML report.")
    sp.add_argument("document")
    sp.add_argument("--out", required=True)
    sp.add_argument("--title", default=None)

    sp = add("seal", "Record the results in the seal ledger.")
    sp.add_argument("document")
    sp.add_argument("--note", default="")

    sp = add("history", "List recorded seals.")
    sp.add_argument("--json", action="store_true")

    sp = add("verify", "Verify the seal ledger hash chain.")
    sp.add_argument("--json", action="store_true")

    add("selftest", "Run the built-in checks.")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    now, argv = extract_now(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage(sys.stderr)
        return 2
    store = resolve_store(getattr(args, "store", None))
    try:
        if args.command == "check":
            return cmd_check(args.document, args.json)
        if args.command == "compute":
            return cmd_compute(args.document, args.json)
        if args.command == "render":
            return cmd_render(args.document, args.out, args.title)
        if args.command == "seal":
            return cmd_seal(args.document, args.note, store, now)
        if args.command == "history":
            return cmd_history(args.json, store)
        if args.command == "verify":
            return cmd_verify(args.json, store)
        if args.command == "selftest":
            return cmd_selftest()
        fail("unknown command %r" % args.command)
    except AlmanacError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
