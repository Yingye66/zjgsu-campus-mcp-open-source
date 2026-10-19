"""Semester-credit CLI regression checks; synthetic sessions and HTTP only."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

import query_semester_credits as cli
from src.auth import session as S
from src.service.jwweb_service import JWWebService
from test.helpers import temp_dir


class FakeHTTP:
    def __init__(self, rows=(), *, term="2091", semester="12", practical=False):
        self.rows = list(rows)
        self.term, self.semester, self.practical = term, semester, practical
        self.calls = []

    def set_cookies(self, cookies):
        pass

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        page = ('<select id="xnm"><option value="2099">2099-2100</option>'
                f'<option value="{self.term}" selected>{self.term}-2092</option></select>'
                f'<select id="xqm"><option value="{self.semester}" selected>学期</option></select>')
        return {"kind": "response", "status": 200, "text": page}

    def post_form(self, url, form, **kwargs):
        self.calls.append(("POST", url, dict(form)))
        body = {"kbList": self.rows}
        if self.practical:
            body["sjkList"] = [{"kcmc": "synthetic practical", "qsjsz": "1-2"}]
        return {"kind": "response", "status": 200, "text": json.dumps(body)}


def row(code, credit="1"):
    return {"kch": code, "kcmc": "Synthetic course", "xf": credit}


class SemesterCreditCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = temp_dir()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)

    def authenticate(self):
        S.save_session(self.data_dir, "acct-synthetic", {
            "zjgsu_token": "SYNTHETIC_SECRET_NEVER_PRINT", "user_type": "2",
            "jwglxt_cookies": [{"name": "JSESSIONID", "value": "SYNTHETIC_COOKIE",
                                "domain": "jwxt.zjgsu.edu.cn", "path": "/"}],
        })
        S.set_active(self.data_dir, "acct-synthetic")

    def run_cli(self, argv, http):
        output = StringIO()
        with patch.object(cli, "default_data_dir", return_value=self.data_dir), \
             patch("src.service.jwweb_service.JwwebTransport", return_value=http), \
             redirect_stdout(output):
            result = cli.main(argv)
        self.assertNotIn("SYNTHETIC_SECRET_NEVER_PRINT", output.getvalue())
        self.assertNotIn("SYNTHETIC_COOKIE", output.getvalue())
        return result, output.getvalue()

    def test_explicit_term_drains_local_pages_without_refetch(self):
        self.authenticate()
        http = FakeHTTP([row(f"SYN{i:04d}") for i in range(501)])
        code, text = self.run_cli(["2026", "3"], http)
        self.assertEqual(code, 0)
        self.assertIn("501 门", text)
        self.assertIn("已知学分小计：501", text)
        self.assertIn("课表可见课程", text)
        self.assertEqual([c[0] for c in http.calls], ["GET", "POST"])
        self.assertEqual(http.calls[-1][2]["xnm"], "2026")

    def test_no_arguments_uses_school_selected_term_not_local_month(self):
        self.authenticate()
        http = FakeHTTP([row("SYN101", "3")])
        code, text = self.run_cli([], http)
        self.assertEqual(code, 0)
        self.assertIn("2091-2092", text)
        self.assertEqual(http.calls[-1][2]["xnm"], "2091")
        self.assertEqual(http.calls[-1][2]["xqm"], "12")

    def test_invalid_arguments_fail_before_data_access_or_http(self):
        for argv in (["2026"], ["2026", "4"], ["２０２６", "3"],
                     ["2026\n", "3"], ["2026", "3", "extra"]):
            with self.subTest(argv=argv), \
                 patch.object(cli, "default_data_dir", side_effect=AssertionError("data accessed")), \
                 redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.main(argv)
                self.assertEqual(error.exception.code, 2)

    def test_unauthenticated_run_never_requests_school(self):
        for argv in ([], ["2026", "3"]):
            http = FakeHTTP()
            code, text = self.run_cli(argv, http)
            self.assertEqual(code, 2)
            self.assertIn("会话", text)
            self.assertEqual(http.calls, [])

    def test_active_marker_without_valid_session_never_requests_school(self):
        S.set_active(self.data_dir, "acct-synthetic")
        for argv in ([], ["2026", "3"]):
            http = FakeHTTP()
            code, text = self.run_cli(argv, http)
            self.assertEqual(code, 2)
            self.assertEqual(http.calls, [])

    def test_import_does_not_initialize_data_or_read_session(self):
        with patch("src.paths.default_data_dir", side_effect=AssertionError("data accessed")), \
             patch("src.storage.store.SnapshotStore", side_effect=AssertionError("store initialized")), \
             patch("src.auth.session.get_active", side_effect=AssertionError("session read")):
            namespace = runpy.run_path(str(Path(cli.__file__)), run_name="import_probe")
        self.assertTrue(callable(namespace["main"]))

    def test_missing_or_invalid_selected_term_does_not_guess(self):
        self.authenticate()
        http = FakeHTTP([row("SYN101")], semester="99")
        code, text = self.run_cli([], http)
        self.assertEqual(code, 1)
        self.assertIn("显式", text)
        self.assertEqual([c[0] for c in http.calls], ["GET"])

    def test_missing_credit_and_practical_block_are_reported_as_gaps(self):
        self.authenticate()
        http = FakeHTTP([row("SYN101", None), row("SYN102", "2"),
                         row("SYN103", "1"), row("SYN103", "5")], practical=True)
        code, text = self.run_cli(["2026", "3"], http)
        self.assertEqual(code, 0)
        self.assertIn("已知学分小计：2", text)
        self.assertIn("学分缺失或无效：SYN101", text)
        self.assertIn("学分冲突：SYN103", text)
        self.assertIn("缺口", text)
        self.assertNotIn("SYN101: 0", text)

    def test_partial_service_result_is_summarized_with_explicit_warning(self):
        self.authenticate()
        http = FakeHTTP([row("SYN101", "3")])
        original = JWWebService.get_timetable_web

        def mark_partial(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            result["status"] = "partial"
            result["issues"].append({"code": "upstream_error", "safe_message": "PRIVATE_NOT_PRINTED"})
            return result

        with patch.object(JWWebService, "get_timetable_web", mark_partial):
            code, text = self.run_cli(["2026", "3"], http)
        self.assertEqual(code, 0)
        self.assertIn("已知学分小计：3", text)
        self.assertIn("partial", text)
        self.assertIn("缺口", text)
        self.assertNotIn("PRIVATE_NOT_PRINTED", text)

    def test_invalid_cursor_stops_without_refetching_first_page(self):
        self.authenticate()
        http = FakeHTTP([row(f"SYN{i:04d}") for i in range(501)])
        original = JWWebService.get_timetable_web

        def corrupt_cursor(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            if not kwargs.get("cursor"):
                result["pagination"]["next_cursor"] = "invalid-local-cursor"
            return result

        with patch.object(JWWebService, "get_timetable_web", corrupt_cursor):
            code, text = self.run_cli(["2026", "3"], http)
        self.assertEqual(code, 1)
        self.assertIn("500 门", text)
        self.assertIn("snapshot_expired", text)
        self.assertIn("缺口", text)
        self.assertEqual([c[0] for c in http.calls], ["GET", "POST"])

    def test_changed_snapshot_does_not_mix_second_page_into_subtotal(self):
        self.authenticate()
        http = FakeHTTP([row(f"SYN{i:04d}") for i in range(501)])
        original = JWWebService.get_timetable_web

        def changed_snapshot(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            if kwargs.get("cursor"):
                result["pagination"]["result_snapshot_id"] = "different-snapshot"
            return result

        with patch.object(JWWebService, "get_timetable_web", changed_snapshot):
            code, text = self.run_cli(["2026", "3"], http)
        self.assertEqual(code, 1)
        self.assertIn("500 门", text)
        self.assertIn("snapshot_changed", text)
        self.assertIn("缺口", text)
        self.assertEqual([c[0] for c in http.calls], ["GET", "POST"])

    def test_missing_next_cursor_reports_gap_without_restarting_query(self):
        self.authenticate()
        http = FakeHTTP([row(f"SYN{i:04d}") for i in range(501)])
        original = JWWebService.get_timetable_web

        def missing_cursor(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            result["pagination"]["next_cursor"] = None
            return result

        with patch.object(JWWebService, "get_timetable_web", missing_cursor):
            code, text = self.run_cli(["2026", "3"], http)
        self.assertEqual(code, 1)
        self.assertIn("500 门", text)
        self.assertIn("invalid_pagination", text)
        self.assertEqual([c[0] for c in http.calls], ["GET", "POST"])


if __name__ == "__main__":
    unittest.main()
