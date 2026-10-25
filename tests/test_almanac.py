import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOL = Path(__file__).parent.parent / "almanac.py"


def run(*args, store=None, env=None):
    cmd = [sys.executable, str(TOOL), *args]
    e = os.environ.copy()
    if env:
        e.update(env)
    if store:
        e["ALMANAC_STORE"] = str(store)
    return subprocess.run(cmd, capture_output=True, text=True, env=e)


CLEAN = textwrap.dedent("""\
    title "Q2 deadlines"
    weekend sat sun
    holiday 2026-03-05 "Team offsite"
    add 10 business days to 2026-03-02
    add 14 calendar days to 2026-03-02
    add 0 business days to 2026-03-02
    between 2026-03-02 and 2026-03-16 business days
    between 2026-03-02 and 2026-03-16 calendar days
    next friday after 2026-03-02
    check 2026-03-06 is business
    check 2026-03-07 is nonworking
    """)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="almanac-e2e-"))
        self.store = self.td / "ledger.jsonl"

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def doc(self, text, name="calendar.almanac"):
        p = self.td / name
        p.write_text(text, encoding="utf-8")
        return p


class TestSelfTest(Base):
    def test_selftest_passes(self):
        proc = run("selftest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("all checks passed", proc.stdout)


class TestCLI(Base):
    def test_version(self):
        proc = run("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("almanac", proc.stdout)

    def test_subcommand_version(self):
        proc = run("compute", "--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1.0.0", proc.stdout)

    def test_help(self):
        self.assertEqual(run("--help").returncode, 0)

    def test_unknown_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL), "frobnicate"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_no_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


class TestCheck(Base):
    def test_clean_exits_zero(self):
        proc = run("check", str(self.doc(CLEAN)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_bad_date_exits_one(self):
        proc = run("check", str(self.doc("add 1 business days to 2026-02-30\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad-date", proc.stdout)

    def test_no_queries_exits_one(self):
        proc = run("check", str(self.doc("; empty\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no-queries", proc.stdout)

    def test_duplicate_holiday_exits_one(self):
        proc = run("check", str(self.doc(
            "holiday 2026-01-01\nholiday 2026-01-01\nadd 1 business days to 2026-01-04\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("duplicate-holiday", proc.stdout)

    def test_date_range_exits_one(self):
        proc = run("check", str(self.doc("between 2026-04-01 and 2026-03-02 business days\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("date-range", proc.stdout)

    def test_check_failed_exits_one(self):
        proc = run("check", str(self.doc("check 2026-03-07 is business\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("check-failed", proc.stdout)

    def test_parse_error_exits_two(self):
        proc = run("check", str(self.doc("frobnicate x\n")))
        self.assertEqual(proc.returncode, 2)

    def test_json_shape(self):
        proc = run("check", str(self.doc(CLEAN)), "--json")
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("findings", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["queries"], 8)

    def test_reads_stdin(self):
        p = subprocess.Popen([sys.executable, str(TOOL), "check", "-"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(input=CLEAN)
        self.assertEqual(p.returncode, 0, err)

    def test_tolerates_utf8_bom(self):
        proc = run("check", str(self.doc("\ufeff" + CLEAN)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TestCompute(Base):
    def test_compute_exits_zero_on_clean(self):
        proc = run("compute", str(self.doc(CLEAN)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("2026-03-17", proc.stdout)
        self.assertIn("holds", proc.stdout)

    def test_compute_with_findings_exits_one(self):
        proc = run("compute", str(self.doc("between 2026-04-01 and 2026-03-02 business days\n")))
        self.assertEqual(proc.returncode, 1)

    def test_compute_json_has_results(self):
        proc = run("compute", str(self.doc(CLEAN)), "--json")
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data["adds"]), 3)
        self.assertEqual(len(data["spans"]), 2)
        self.assertEqual(len(data["nexts"]), 1)
        self.assertEqual(len(data["checks"]), 2)
        adds = {a["text"]: a["result"] for a in data["adds"]}
        self.assertEqual(adds["add 10 business days to 2026-03-02"], "2026-03-17")
        self.assertEqual(adds["add 0 business days to 2026-03-02"], "2026-03-02")

    def test_holiday_delays(self):
        proc = run("compute", str(self.doc(CLEAN)))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-17", proc.stdout)

    def test_custom_weekend(self):
        proc = run("compute", str(self.doc(
            "weekend mon tue\nadd 1 business days to 2026-03-02\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-04", proc.stdout)

    def test_leap_day(self):
        proc = run("compute", str(self.doc(
            "add 1 calendar days to 2028-02-28\nadd 2 calendar days to 2028-02-28\n")))
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout
        self.assertIn("2028-02-29", out)
        self.assertIn("2028-03-01", out)

    def test_next_strictly_after(self):
        proc = run("compute", str(self.doc("next friday after 2026-03-06\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-13", proc.stdout)


class TestRender(Base):
    def test_render_writes_file(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc(CLEAN)), "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.exists())
        content = out.read_text(encoding="utf-8")
        self.assertIn("add 10 business days", content)
        self.assertIn("2026-03-17", content)
        self.assertNotIn("http://", content)
        self.assertNotIn("https://", content)
        self.assertNotIn("<script", content)

    def test_render_empty_document(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc("; empty\n")), "--out", str(out))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("No dates to resolve", out.read_text(encoding="utf-8"))

    def test_render_broken_document_still_exits_zero(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc("add 1 business days to 2026-02-30\n")),
                   "--out", str(out))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("bad-date", out.read_text(encoding="utf-8"))

    def test_render_title_override(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc(CLEAN)), "--out", str(out),
                   "--title", "Q2 Deadlines")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Q2 Deadlines", out.read_text(encoding="utf-8"))


class TestSealLedger(Base):
    def test_seal_clean(self):
        proc = run("seal", str(self.doc(CLEAN)), store=self.store)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.store.exists())
        self.assertIn("recorded as seq 0", proc.stdout)
        recs = self.store.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(recs), 1)
        rec = json.loads(recs[0])
        self.assertEqual(rec["document"], "calendar.almanac")
        self.assertEqual(rec["holidays"], 1)
        self.assertEqual(rec["adds"], 3)
        self.assertEqual(rec["spans"], 2)
        self.assertEqual(rec["checks"], 2)
        self.assertEqual(len(rec["document_hash"]), 64)
        self.assertEqual(len(rec["results_hash"]), 64)

    def test_seal_refuses_findings(self):
        proc = run("seal", str(self.doc("add 1 business days to 2026-02-30\n")),
                   store=self.store)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to seal", proc.stdout)

    def test_seal_deterministic_timestamp(self):
        run("seal", str(self.doc(CLEAN)), "--now", "2026-01-01T00:00:00",
            store=self.store)
        rec = json.loads(self.store.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["ts"], "2026-01-01T00:00:00")

    def test_history_json(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        proc = run("history", "--json", store=self.store)
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["adds"], 3)

    def test_verify_clean(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        run("seal", str(self.doc(CLEAN)), store=self.store)
        proc = run("verify", store=self.store)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("chain intact", proc.stdout)

    def test_verify_detects_an_edit(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        text = self.store.read_text(encoding="utf-8").replace(
            '"note":""', '"note":"forged"')
        self.store.write_text(text, encoding="utf-8")
        proc = run("verify", store=self.store)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()


class TestMore(Base):
    def test_add_business_holiday_skipped(self):
        proc = run("compute", str(self.doc(
            "holiday 2026-03-03\nadd 2 business days to 2026-03-02\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-05", proc.stdout)

    def test_add_calendar_singular(self):
        proc = run("compute", str(self.doc("add 1 calendar day to 2026-03-02\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-03", proc.stdout)

    def test_between_calendar(self):
        proc = run("compute", str(self.doc(
            "between 2026-03-02 and 2026-03-09 calendar days\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("= 7", proc.stdout)

    def test_next_tuesday(self):
        proc = run("compute", str(self.doc("next tuesday after 2026-03-02\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-03", proc.stdout)

    def test_next_monday_strict(self):
        proc = run("compute", str(self.doc("next monday after 2026-03-02\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-09", proc.stdout)

    def test_check_business_holds(self):
        proc = run("check", str(self.doc("check 2026-03-02 is business\n")))
        self.assertEqual(proc.returncode, 0)

    def test_weekend_custom_fri_sat(self):
        proc = run("compute", str(self.doc(
            "weekend fri sat\nadd 1 business days to 2026-03-02\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-03-03", proc.stdout)

    def test_bad_weekday_in_next_exits_one(self):
        proc = run("compute", str(self.doc("next funday after 2026-03-02\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad-weekday", proc.stdout)

    def test_negative_count_exits_one(self):
        proc = run("compute", str(self.doc("add -5 business days to 2026-03-02\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad-count", proc.stdout)

    def test_duplicate_weekend_exits_one(self):
        proc = run("compute", str(self.doc(
            "weekend sat sun\nweekend fri\nadd 1 business days to 2026-03-02\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("duplicate-weekend", proc.stdout)

    def test_holiday_label_parses(self):
        proc = run("compute", str(self.doc(
            "holiday 2026-12-25 \"Christmas\"\nadd 1 business days to 2026-12-24\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2026-12-28", proc.stdout)

    def test_leap_year_feb(self):
        proc = run("compute", str(self.doc("add 1 calendar days to 2028-02-29\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2028-03-01", proc.stdout)
