"""Semester-credit route: one source call, full-snapshot aggregation and timings."""
import importlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test.helpers import Clock, seed_session, temp_dir
from src.auth import session as S
from src.service.jwweb_service import JWWebService
from src.storage.store import SnapshotStore


class TimetableHTTP:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def set_cookies(self, cookies):
        pass

    def get(self, url, headers=None, timeout=20):
        self.calls.append("GET")
        return {"kind": "response", "status": 200, "text": "<html>index</html>"}

    def post_form(self, url, form=None, headers=None, timeout=20):
        self.calls.append("POST")
        return {"kind": "response", "status": 200,
                "text": json.dumps({"kbList": self.rows, "sjkList": []})}


class TimetableCreditRouteTest(unittest.TestCase):
    def setUp(self):
        temporary = temp_dir()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        seed_session(self.data, "acct-credit-route-synthetic")
        session = S.load_session(self.data, "acct-credit-route-synthetic")
        session["jwglxt_cookies"] = [{"name": "JSESSIONID", "value": "synthetic",
                                     "domain": "jwxt.zjgsu.edu.cn", "path": "/",
                                     "secure": False, "expires": None, "discard": True}]
        S.save_session(self.data, "acct-credit-route-synthetic", session)
        self.http = TimetableHTTP([{"kch": "C%d" % i, "kcmc": "Synthetic course %d" % i,
                                     "xf": "0.1", "xqj": "1"} for i in range(60)])
        self.store = SnapshotStore(self.data / "cache.sqlite3")
        self.service = JWWebService(self.store, self.data, jwweb_transport=self.http,
                                     now_fn=Clock().now)

    def test_summary_uses_whole_local_snapshot_not_first_page(self):
        result = self.service.get_timetable_web("2026", "3", limit=10)
        self.assertEqual(len(result["items"]), 10)
        self.assertTrue(result["pagination"]["has_more"])
        self.assertEqual(result["credit_summary"]["visible_course_count"], 60)
        self.assertEqual(result["credit_summary"]["known_credit_sum"], "6")
        self.assertFalse(result["credit_summary"]["source_complete"])
        self.assertEqual(self.http.calls, ["GET", "POST"])

    def test_numeric_source_fields_pass_sdk_schema_and_timing_is_safe(self):
        self.http.rows = [{"kch": "C1", "kcmc": "Example", "xf": 3,
                           "xqj": 2, "xkrs": 20}]
        result = self.service.get_timetable_web("2026", "3")
        self.assertEqual(result["items"][0]["credit"], "3")
        self.assertEqual(result["items"][0]["weekday"], "2")
        self.assertEqual(result["items"][0]["enrolled"], "20")
        timings = result["performance"]["source_query"]
        self.assertEqual(timings["http_request_count"], 2)
        self.assertEqual([r["method"] for r in timings["requests"]], ["GET", "POST"])
        self.assertGreaterEqual(timings["total_ms"], timings["http_ms"])
        self.assertNotIn("synthetic", json.dumps(timings))

    def test_mcp_hit_retains_summary_but_reports_current_call_separately(self):
        with patch.dict(os.environ, {"ZJGSU_CAMPUS_DATA_DIR": str(self.data)}):
            server = importlib.import_module("src.mcp_server.server")
        from src.service.query_cache import QueryCache
        from jsonschema import validate
        with patch.object(server, "jwweb_service", self.service), patch.object(server, "query_cache", QueryCache()):
            first = server.get_timetable_web("2026", "3").structured_content
            second_result = server.get_timetable_web("2026", "3")
        second = second_result.structured_content
        self.assertTrue(second["freshness"]["cache_hit"])
        self.assertEqual(second["credit_summary"], first["credit_summary"])
        self.assertEqual(second["performance"]["current_call"]["http_request_count"], 0)
        self.assertEqual(second["performance"]["source_query"]["http_request_count"], 2)
        self.assertEqual(self.http.calls, ["GET", "POST"])
        self.assertEqual(json.loads(second_result.content[0].text), second)
        # TypedDict schema is the same contract consumed by MCP.
        from pydantic import TypeAdapter
        from src.domain.models import TimetableWebObservation
        validate(second, TypeAdapter(TimetableWebObservation).json_schema())

    def test_cursor_summaries_use_fixed_whole_snapshot_without_more_http(self):
        first = self.service.get_timetable_web("2026", "3", limit=10)
        second = self.service.get_timetable_web("2026", "3", limit=10,
                                              cursor=first["pagination"]["next_cursor"])
        self.assertEqual(second["credit_summary"], first["credit_summary"])
        self.assertEqual(second["performance"]["source_query"]["http_request_count"], 0)
        self.assertEqual(self.http.calls, ["GET", "POST"])


if __name__ == "__main__":
    unittest.main()
