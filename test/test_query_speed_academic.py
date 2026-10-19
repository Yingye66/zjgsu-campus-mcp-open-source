# -*- coding: utf-8 -*-
"""Query reuse through real academic services, adapters and temporary storage.

Only HTTP is replaced: routes reject unknown endpoints and check term, function
activation, pagination and course identifiers. No school request or real session
is used. Request counts express the speed regression without timing assertions.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import jsonschema
from pydantic import TypeAdapter

from test.helpers import Clock, FakeTransport, course_item, course_ok, temp_dir
from src.adapters.campus.jwweb import (
    GRADE_GNM, GRADE_INDEX_PATH, GRADE_KC_PATH, GRADE_MX_GNM,
    GRADE_MX_INDEX_PATH, GRADE_PATH, GRADE_XM_PATH, JWXT_BASE,
    TIMETABLE_GNM, TIMETABLE_INDEX_PATH, TIMETABLE_PATH,
)
from src.auth import session as S
from src.domain.models import (
    GradeDetailsObservation, GradesObservation, TimetableWebObservation,
)
from src.service.jwweb_service import JWWebService
from src.service.campus_service import CampusService
from src.service.query_cache import QueryCache
from src.storage.store import SnapshotStore


def _response(body):
    return {"kind": "response", "status": 200,
            "text": json.dumps(body, ensure_ascii=False)}


def _page(items):
    return {"items": items, "currentPage": 1, "totalPage": 1,
            "totalCount": len(items), "totalResult": len(items)}


class AcademicHTTP:
    """Strict endpoint double; the production parser and service remain real."""

    def __init__(self):
        self.calls = []
        self.activated = None
        self.score = "88"
        self.room = "示例楼 A101"
        self.component_score = "91"

    def set_cookies(self, cookies):
        if not isinstance(cookies, list) or not cookies:
            raise AssertionError("academic request requires a seeded cookie list")

    def get(self, url, headers=None, timeout=20):
        path = urlsplit(url).path
        expected = {GRADE_INDEX_PATH: GRADE_GNM,
                    TIMETABLE_INDEX_PATH: TIMETABLE_GNM,
                    GRADE_MX_INDEX_PATH: GRADE_MX_GNM}
        if path not in expected or parse_qs(urlsplit(url).query) != {
                "gnmkdm": [expected[path]]}:
            raise AssertionError("unexpected activation route: %s" % url)
        self.activated = expected[path]
        self.calls.append(("GET", path, None))
        return {"kind": "response", "status": 200, "text": "<html>index</html>"}

    def post_form(self, url, form=None, headers=None, timeout=20):
        parts = urlsplit(url)
        if parts.scheme + "://" + parts.netloc != JWXT_BASE:
            raise AssertionError("unexpected academic host")
        form = dict(form or {})
        if (form.get("xnm"), form.get("xqm")) != ("2026", "3"):
            raise AssertionError("academic term was not forwarded")
        expected_gnm = {GRADE_PATH: GRADE_GNM, TIMETABLE_PATH: TIMETABLE_GNM,
                        GRADE_KC_PATH: GRADE_MX_GNM, GRADE_XM_PATH: GRADE_MX_GNM}
        if parts.path not in expected_gnm:
            raise AssertionError("unexpected academic query path: %s" % parts.path)
        gnm = expected_gnm[parts.path]
        if self.activated != gnm or parse_qs(parts.query).get("gnmkdm") != [gnm]:
            raise AssertionError("query must follow the matching function activation")
        if parts.path != TIMETABLE_PATH:
            if (form.get("queryModel.currentPage"),
                    form.get("queryModel.showCount")) != ("1", "100"):
                raise AssertionError("missing source pagination parameters")
            if parse_qs(parts.query).get("doType") != ["query"]:
                raise AssertionError("only query requests are supported")
        self.calls.append(("POST", parts.path, form))
        if parts.path == GRADE_PATH:
            return _response(_page([{
                "kcmc": "示例数据库", "kch": "DB101", "cj": self.score,
                "jd": "3.8", "xf": "3.0", "jsxm": "示例教师",
                "jxbmc": "synthetic-grade-class",
            }]))
        if parts.path == TIMETABLE_PATH:
            return _response({"kbList": [{
                "kcmc": "示例数据库", "kch": "DB101", "jxbmc": "synthetic-class",
                # This fixture covers the verified string-valued source shape.
                # Numeric source weekday normalization is outside cache coverage.
                "jc": "3-4节", "jcs": "3-4", "xqj": "2", "xqjmc": "星期二",
                "zcd": "1-16周", "cdmc": self.room, "jgh": "FAKE-T01",
                "xm": "示例教师", "xf": "3.0",
            }], "sjkList": []})
        if parts.path == GRADE_KC_PATH:
            return _response(_page([
                {"kcmc": "示例数据库", "kch_id": "DB101", "jxb_id": "CLASS-DB",
                 "jxbmc": "synthetic-db", "zpcj": "88", "xf": "3.0"},
                {"kcmc": "示例程序设计", "kch_id": "CS102", "jxb_id": "CLASS-CS",
                 "jxbmc": "synthetic-cs", "zpcj": "82", "xf": "2.0"},
            ]))
        identifiers = (form.get("kch_id"), form.get("jxb_id"))
        if identifiers not in (("DB101", "CLASS-DB"), ("CS102", "CLASS-CS")):
            raise AssertionError("course details lost their source identifiers")
        score = self.component_score if identifiers[0] == "DB101" else "79"
        return _response(_page([{"xmblmc": "期末(50%)", "xmcj": score}]))


class AcademicQueryReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        S.save_session(self.path, "synthetic-speed-account", {
            "schema": 1, "zjgsu_token": "SYNTHETIC-NOT-A-TOKEN", "user_type": "2",
            "jwglxt_cookies": [{"name": "JSESSIONID", "value": "SYNTHETIC",
                                 "domain": "jwxt.zjgsu.edu.cn", "path": "/jwglxt",
                                 "secure": False, "expires": None, "discard": True}],
        })
        S.set_active(self.path, "synthetic-speed-account")
        self.clock = Clock()
        self.transport = AcademicHTTP()
        self.service = JWWebService(SnapshotStore(self.path / "cache.sqlite3"),
                                    self.path, now_fn=self.clock.now,
                                    jwweb_transport=self.transport)
        self.monotonic = 100.0
        self.cache = QueryCache(ttl_seconds=60, max_entries=32,
                                monotonic_fn=lambda: self.monotonic)

    def call(self, method, **kwargs):
        return self.cache.call(method, "2026", "3", **kwargs)

    def test_grades_repeat_avoids_http_and_force_refresh_gets_new_score(self):
        first = self.call(self.service.get_grades)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["items"][0]["score"], "88")
        self.assertEqual(len(self.transport.calls), 2)
        repeated = self.call(self.service.get_grades)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(repeated["freshness"]["cache_hit"])
        self.assertEqual(repeated["items"], first["items"])
        self.transport.score = "95"
        refreshed = self.call(self.service.get_grades, force_refresh=True)
        self.assertEqual(len(self.transport.calls), 4)
        self.assertFalse(refreshed["freshness"]["cache_hit"])
        self.assertEqual(refreshed["items"][0]["score"], "95")
        self.assertNotEqual(first["pagination"]["result_snapshot_id"],
                            refreshed["pagination"]["result_snapshot_id"])

    def test_timetable_repeat_avoids_http_and_refresh_gets_new_room(self):
        first = self.call(self.service.get_timetable_web)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["items"][0]["weekday"], "2")
        self.assertEqual(first["items"][0]["teacher_name"], "示例教师")
        repeated = self.call(self.service.get_timetable_web)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(repeated["freshness"]["cache_hit"])
        self.transport.room = "示例楼 B202"
        refreshed = self.call(self.service.get_timetable_web, force_refresh=True)
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(refreshed["items"][0]["room"], "示例楼 B202")
        self.assertFalse(refreshed["freshness"]["cache_hit"])

    def test_grade_details_reuse_preserves_course_filter_and_component_arguments(self):
        all_courses = self.call(self.service.get_grade_details)
        self.assertEqual([row["course_code"] for row in all_courses["items"]],
                         ["DB101", "CS102"])
        self.assertEqual(len(self.transport.calls), 4)
        self.assertTrue(self.call(self.service.get_grade_details)["freshness"]["cache_hit"])
        self.assertEqual(len(self.transport.calls), 4)
        filtered = self.call(self.service.get_grade_details, course="db101")
        self.assertEqual(len(self.transport.calls), 7)
        self.assertEqual([row["course_code"] for row in filtered["items"]], ["DB101"])
        self.assertEqual([{key: component[key] for key in ("component_name", "component_score")}
                          for component in filtered["items"][0]["components"]],
                         [{"component_name": "期末(50%)", "component_score": "91"}])
        self.assertFalse(filtered["items"][0]["components_failed"])
        self.assertTrue(self.call(self.service.get_grade_details, course="db101")
                        ["freshness"]["cache_hit"])
        self.assertEqual(len(self.transport.calls), 7)
        self.transport.component_score = "97"
        refreshed = self.call(self.service.get_grade_details, course="db101", force_refresh=True)
        self.assertEqual(len(self.transport.calls), 10)
        self.assertEqual(refreshed["items"][0]["components"][0]["component_score"], "97")
        self.assertEqual(self.transport.calls[-1][2]["jxb_id"], "CLASS-DB")

    def test_cache_hit_keeps_source_time_and_ages_data_without_network(self):
        first = self.call(self.service.get_grades)
        self.clock.advance(17)
        self.monotonic += 17
        repeated = self.call(self.service.get_grades)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(repeated["freshness"]["last_success_at"],
                         first["freshness"]["last_success_at"])
        self.assertEqual(repeated["source_refs"], first["source_refs"])
        self.assertEqual(repeated["freshness"]["data_age_seconds"], 17)
        self.assertEqual(repeated["freshness"]["by_source"][0]["data_age_seconds"], 17)
        self.assertEqual(repeated["freshness"]["cache_remaining_seconds"], 43)

    def test_positional_and_named_term_arguments_share_the_same_query(self):
        self.call(self.service.get_grades)
        repeated = self.cache.call(self.service.get_grades, xnm="2026", xqm="3")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(repeated["freshness"]["cache_hit"])

    def test_fresh_and_reused_academic_results_match_output_schemas(self):
        for method, model in ((self.service.get_grades, GradesObservation),
                              (self.service.get_timetable_web, TimetableWebObservation),
                              (self.service.get_grade_details, GradeDetailsObservation)):
            with self.subTest(tool=method.__name__):
                schema = TypeAdapter(model).json_schema()
                first = self.call(method)
                repeated = self.call(method)
                self.assertEqual(first["status"], "ok")
                self.assertFalse(first["freshness"]["cache_hit"])
                self.assertTrue(repeated["freshness"]["cache_hit"])
                for observation in (first, repeated):
                    jsonschema.validate(observation, schema)
                    self.assertIsInstance(observation["freshness"]["cache_hit"], bool)
                    self.assertIsInstance(observation["freshness"]["cache_ttl_seconds"], int)
                    self.assertEqual(observation["pagination"]["result_snapshot_id"],
                                     first["pagination"]["result_snapshot_id"])

    def test_registered_tools_expose_refresh_and_validate_reused_freshness(self):
        # Import-time store initialization is confined to this temporary account.
        with patch.dict(os.environ, {"ZJGSU_CAMPUS_DATA_DIR": str(self.path)}):
            server = importlib.import_module("src.mcp_server.server")
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        native_http = FakeTransport([course_ok([course_item()])])
        native = CampusService(self.service.store, self.path, transport=native_http,
                               now_fn=self.clock.now)
        calls = [("get_timetable", ("2026-10-12", "2026-10-18")),
                 ("get_grades", ("2026", "3")),
                 ("get_timetable_web", ("2026", "3")),
                 ("get_grade_details", ("2026", "3"))]
        with patch.object(server, "service", native), \
                patch.object(server, "jwweb_service", self.service), \
                patch.object(server, "query_cache", self.cache):
            for name, arguments in calls:
                with self.subTest(tool=name):
                    tool = tools[name]
                    refresh = tool.input_schema["properties"]["force_refresh"]
                    self.assertEqual(refresh["type"], "boolean")
                    self.assertIs(refresh["default"], False)
                    self.assertNotIn("force_refresh", tool.input_schema.get("required", []))
                    first = getattr(server, name)(*arguments)
                    repeated = getattr(server, name)(*arguments)
                    self.assertFalse(first.structured_content["freshness"]["cache_hit"])
                    self.assertTrue(repeated.structured_content["freshness"]["cache_hit"])
                    self.assertIsNotNone(tool.output_schema)
                    for result in (first, repeated):
                        jsonschema.validate(result.structured_content, tool.output_schema)
                        self.assertEqual(json.loads(result.content[0].text),
                                         result.structured_content)
                        self.assertFalse(result.is_error)
        self.assertEqual(len(native_http.calls), 1)


if __name__ == "__main__":
    unittest.main()
