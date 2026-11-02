# -*- coding: utf-8 -*-
"""MCP 集成测试：真实 stdio 子进程 + SDK 客户端（本机合成数据，不触网）。

验证（对应「注册工具 + 真实发现/调用」的实施层检查；accepted 的最终判定
需在目标 MCP 客户端中完成并另行记录）：
- 工具发现：4 个工具在列（含 get_campus_events），且带 outputSchema；
- 只读调用（cursor 路径，不触发上游请求）返回不报错的 structuredContent；
- 事件（公共域）不需要登录会话即可读缓存；课表未初始化时返回 auth_required。

受限环境说明：stdio 集成测试需要允许创建子进程且临时目录可写。
- 若系统 TEMP 不可写：测试默认改用项目内 `validation/test-temp`（可用 CAMPUS_TEST_TMP_BASE 覆盖）；
- 若环境完全不允许子进程：设置 `CAMPUS_SKIP_STDIO_TEST=1` 显式跳过（输出中会显示 skipped 原因），
  离线单元测试不受影响。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

from test.helpers import PROJECT_ROOT, event_item, seed_session, temp_dir

from src.adapters.campus.schema import validate_and_map
from src.adapters.career.events import SOURCE_JYW_XJH, map_event
from src.domain.models import (ACCOUNT_PUBLIC, MODULE_EVENTS, MODULE_TIMETABLE,
                               SOURCE_COURSE_API)
from src.service.campus_service import _encode_cursor
from src.storage.store import SnapshotStore

R1 = ("2026-10-12", "2026-10-18")
S1 = "%s~%s" % R1
ED = "2026-10-14"
ES = "%s~%s" % (ED, ED)
ACCT = "acct-cccc000000000003"

SKIP_STDIO = os.environ.get("CAMPUS_SKIP_STDIO_TEST") == "1"


def _entry(entry_id: str, weekday: int = 1) -> dict:
    it = {
        "kcmc": "课程-%s" % entry_id, "xqj": weekday, "ksj": "1", "jsj": "2",
        "jsmc": "B202", "xn": "2026-2027", "xq": "1", "zc": 5, "skrq": "10-12",
        "nyr": "2026-10-12", "kcdm": "CC01", "kclx": "本科生", "jsbh": "00002",
        "id": entry_id,
    }
    entries, violations = validate_and_map([it])
    assert not violations, violations
    return entries[0]


def _event_stored(event_id: str) -> dict:
    e = map_event(event_item(xjhid=event_id), ED)
    assert e is not None
    return e


@unittest.skipIf(SKIP_STDIO, "CAMPUS_SKIP_STDIO_TEST=1：显式跳过 stdio 集成测试")
class _StdioClientMixin:
    async def _run_client(self, data_dir: Path, callback, extra_env: dict | None = None):
        from mcp.client.client import Client
        from mcp.client.stdio import StdioServerParameters

        env = dict(os.environ)
        env["ZJGSU_CAMPUS_DATA_DIR"] = str(data_dir)
        env.update(extra_env or {})
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.mcp_server.server"],
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        try:
            async with Client(params) as client:
                return await callback(client)
        except Exception as e:  # 给出可执行的排查提示，避免「停在首项、原因未定」
            raise RuntimeError(
                "MCP stdio 集成启动/运行失败：%s（若在受限环境中：确认允许子进程与临时目录写权限；"
                "可用 CAMPUS_TEST_TMP_BASE 指定可写目录，或设 CAMPUS_SKIP_STDIO_TEST=1 显式跳过）"
                % e) from e


@unittest.skipIf(SKIP_STDIO, "CAMPUS_SKIP_STDIO_TEST=1：显式跳过 stdio 集成测试")
class McpStdioIntegrationTest(_StdioClientMixin, unittest.IsolatedAsyncioTestCase):
    async def test_seeded_flow(self):
        with temp_dir() as td:
            data = Path(td)
            store = SnapshotStore(data / "cache.sqlite3")
            entries = [_entry("E1"), _entry("E2", weekday=2)]
            snap = store.upsert_snapshot(
                ACCT, MODULE_TIMETABLE, SOURCE_COURSE_API, S1, items=entries,
                fetched_at="2026-10-13T14:00:00+08:00", complete=False,
                consistency="best_effort", missing_ranges=[], empty_evidence=False)
            store.record_attempt(ACCT, MODULE_TIMETABLE, SOURCE_COURSE_API,
                                 "2026-10-13T14:00:00+08:00", S1)
            store.record_success(ACCT, MODULE_TIMETABLE, SOURCE_COURSE_API,
                                 "2026-10-13T14:00:00+08:00", S1)
            seed_session(data, ACCT)
            cursor = _encode_cursor(ACCT, MODULE_TIMETABLE, S1, snap["snapshot_id"], 0)

            # 公共域事件快照（含完整性证据 complete=True；与课表形成对照）
            ev = [_event_stored("EV1"), _event_stored("EV2")]
            esnap = store.upsert_snapshot(
                ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH, ES, items=ev,
                fetched_at="2026-10-13T14:00:00+08:00", complete=True,
                consistency="best_effort", missing_ranges=[], empty_evidence=False)
            store.record_attempt(ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH,
                                 "2026-10-13T14:00:00+08:00", ES)
            store.record_success(ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH,
                                 "2026-10-13T14:00:00+08:00", ES)
            ecursor = _encode_cursor(ACCOUNT_PUBLIC, MODULE_EVENTS, ES, esnap["snapshot_id"], 0)

            async def scenario(client):
                tools = await client.list_tools()
                names = sorted(t.name for t in tools.tools)
                                                                                                                                                                                                                                                                # R44+F24+F28+F04ext：49 工具（新增 get_mp_article_content）（44 验收清单 + get_library_seats + get_dorm_electric + get_training_plan_doc + get_mp_articles）
                self.assertEqual(names, [
                    "get_academic_progress",
                    "get_academic_warnings",
                    "get_campus_events",
                    "get_campus_news",
                    "get_campus_notices",
                    "get_career_info",
                    "get_class_timetable",
                    "get_course_catalog",
                    "get_credit_requirements",
                    "get_digital_resources",
                    "get_dorm_electric",
                    "get_evaluation_result",
                    "get_exam_registrations",
                    "get_exams",
                    "get_fitness_schedule",
                    "get_fitness_scores",
                    "get_free_classrooms",
                    "get_grade_details",
                    "get_grades",
                    "get_innovation_scores",
                    "get_internship_grades",
                    "get_job_fairs",
                    "get_jww_notices",
                    "get_labor_course_detail",
                    "get_labor_courses",
                    "get_library_info",
                    "get_library_search",
                    "get_library_seats",
                    "get_mp_article_content",
                    "get_mp_articles",
                    "get_mp_recent_content",
                    "get_my_approvals",
                    "get_my_messages",
                    "get_my_profile",
                    "get_online_class_urls",
                    "get_preselect_info",
                    "get_retake_courses",
                    "get_scholarship_publications",
                    "get_source_health",
                    "get_student_handbook",
                    "get_syllabus",
                    "get_teacher_page",
                    "get_teaching_plan_courses",
                    "get_teaching_plans",
                    "get_timetable",
                    "get_timetable_web",
                    "get_training_plan_doc",
                    "get_training_plans",
                    "list_campus_capabilities",
                    "list_campus_services"
                ])


                # R53：无会话门禁——教务查询返回 auth_required（不触网）
                result = await client.call_tool(
                    "get_exams", {"xnm": "2025", "xqm": "3"})
                # 无会话门禁：unavailable → is_error=True（设计语义），auth_required 在结构体里
                self.assertEqual(result.is_error, True)
                payload = None
                for blk in (result.content or []):
                    if getattr(blk, "type", "") == "text":
                        payload = json.loads(blk.text)
                        break
                self.assertIsNotNone(payload, "text content 缺失")
                self.assertEqual(payload.get("status"), "unavailable")
                self.assertTrue(any(i.get("code") == "auth_required"
                                    for i in payload.get("issues", [])))

                # R53：公共域 events 有种子快照——正常返回（cursor 路径不触网）
                result_ev = await client.call_tool(
                    "get_campus_events", {"start_date": "2026-10-14",
                                          "end_date": "2026-10-14",
                                          "cursor": _encode_cursor(
                                              ACCOUNT_PUBLIC, MODULE_EVENTS, ES,
                                              esnap["snapshot_id"], 0)})
                self.assertEqual(result_ev.is_error, False)
                ev_payload = None
                for blk in (result_ev.content or []):
                    if getattr(blk, "type", "") == "text":
                        ev_payload = json.loads(blk.text)
                        break
                self.assertIsNotNone(ev_payload, "events text content 缺失")
                self.assertIn(ev_payload.get("status"), ("ok", "partial"))

                # T1-T3（2026-10-16）：新工具 cursor 探针路径（不触网，unavailable 语义）
                for tname, args in [
                    ("get_my_approvals", {"scope": "mine", "cursor": "probe"}),
                    ("get_scholarship_publications", {"cursor": "probe"}),
                    ("get_digital_resources", {"category": "all", "cursor": "probe"}),
                ]:
                    r_probe = await client.call_tool(tname, args)
                    pp = json.loads(r_probe.content[0].text)
                    self.assertEqual(pp.get("status"), "unavailable", tname)
                    self.assertTrue(any(i.get("code") == "invalid_arguments"
                                        for i in pp.get("issues", [])), tname)

            # R53：关键修复——真正 await _run_client（此前只定义 scenario 未执行）
            await self._run_client(data, scenario)


@unittest.skipIf(SKIP_STDIO, "CAMPUS_SKIP_STDIO_TEST=1：显式跳过 stdio 集成测试")
class MpArticlesStdioTest(_StdioClientMixin, unittest.IsolatedAsyncioTestCase):
    """公众号来源圈定 + 加速参数的真实 stdio 合成调用（§2.172 第 3 步、§2.174 复验）。"""

    @staticmethod
    def _payload(result):
        return json.loads(result.content[0].text)

    async def test_recent_content_tool_schema_and_empty_source(self):
        """真实 MCP 调用可发现近期正文工具，错参数拒绝且空来源不伪造结果。"""
        from test.test_mp_articles_scope import build_db

        with temp_dir() as td:
            base = Path(td)
            db = base / "synthetic.db"
            build_db(db)

            async def scenario(client):
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                tool = tools["get_mp_recent_content"]
                self.assertFalse(tool.input_schema.get("additionalProperties", True))
                self.assertIn("coverage", tool.output_schema["properties"])
                invalid = await client.call_tool("get_mp_recent_content", {"limti": 5})
                self.assertTrue(invalid.is_error)
                result = await client.call_tool("get_mp_recent_content", {
                    "scope": "all", "mp_name": "不存在的合成公众号", "days": 365})
                self.assertFalse(result.is_error)
                payload = self._payload(result)
                self.assertEqual(payload, result.structured_content)
                self.assertEqual(payload["data"]["articles"], [])
                self.assertFalse(payload["coverage"]["complete"])

            await self._run_client(base, scenario,
                                   extra_env={"ZJGSU_MP_RSS_DB": str(db),
                                              "ZJGSU_MP_SOURCES": str(base / "missing.json")})

    async def test_unknown_parameters_do_not_silently_widen_article_query(self):
        """拼错筛选参数必须拒绝，不能把它当作未指定条件。"""
        from test.test_mp_articles_scope import build_db

        with temp_dir() as td:
            base = Path(td)
            db = base / "synthetic.db"
            build_db(db)

            async def scenario(client):
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                # 验证协议端真实行为，而不是只检查 Python 函数签名。
                response = await client.call_tool(
                    "get_mp_articles", {"days": 0, "keywrods": ["不存在的词"]})
                self.assertTrue(response.is_error)
                self.assertIsNone(response.structured_content)
                self.assertIn("keywrods", response.content[0].text)
                content_response = await client.call_tool(
                    "get_mp_article_content", {"url": "not-a-url", "max_charz": 500})
                self.assertTrue(content_response.is_error)
                self.assertIn("max_charz", content_response.content[0].text)
                for arguments in ({"days": False}, {"days": "0"}, {"limit": True},
                                  {"count_total": "false"}):
                    with self.subTest(arguments=arguments):
                        invalid = await client.call_tool("get_mp_articles", arguments)
                        self.assertTrue(invalid.is_error)
                        self.assertIsNone(invalid.structured_content)
                self.assertFalse(tools["get_mp_articles"].input_schema.get("additionalProperties", True))
                self.assertFalse(tools["get_mp_article_content"].input_schema.get("additionalProperties", True))

            await self._run_client(base, scenario,
                                   extra_env={"ZJGSU_MP_RSS_DB": str(db),
                                              "ZJGSU_MP_SOURCES": str(base / "missing.json")})

    async def test_scope_and_fast_page_through_sdk(self):
        from test.test_mp_articles_scope import build_db, write_registry

        with temp_dir() as td:
            base = Path(td)
            db = base / "mp.db"
            build_db(db)
            registry = base / "mp_sources.json"
            write_registry(registry, [
                {"id": "feed-jwc", "name": "浙江工商大学教务处",
                 "status": "confirmed", "category": "教务"},
                {"id": "feed-hz", "name": "浙江工商大学杭州商学院", "status": "excluded"},
            ])

            async def scenario(client):
                tools = await client.list_tools()
                tool = [t for t in tools.tools if t.name == "get_mp_articles"][0]
                self.assertEqual(set(tool.input_schema["properties"]),
                                 {"mp_name", "keyword", "days", "limit", "offset",
                                  "scope", "mp_ids", "keywords", "count_total"})
                self.assertIn("scope_info", tool.output_schema["properties"])
                self.assertIn("performance", tool.output_schema["properties"])

                result = await client.call_tool(
                    "get_mp_articles",
                    {"scope": "school", "days": 0, "limit": 50, "count_total": False})
                self.assertFalse(result.is_error)
                payload = self._payload(result)
                # 文本与结构化输出必须同源一致
                self.assertEqual(payload, result.structured_content)
                self.assertEqual(payload["status"], "ok")
                self.assertIsNone(payload["coverage"]["total_matched"])
                self.assertFalse(payload["coverage"]["total_matched_known"])
                self.assertEqual(payload["scope_info"]["scope_sources"]["confirmed"], 1)
                self.assertEqual(payload["scope_info"]["scope_sources"]["excluded"], 1)
                names = {item["mp_name"] for item in payload["data"]["articles"]}
                self.assertEqual(names, {"浙江工商大学", "浙江工商大学教务处",
                                         "浙江工商大学计算机学院"})
                self.assertTrue(all("source_category" in item
                                    for item in payload["data"]["articles"]))
                self.assertIn("performance", payload)

                confirmed = await client.call_tool(
                    "get_mp_articles",
                    {"scope": "school_confirmed", "days": 0, "limit": 50})
                self.assertFalse(confirmed.is_error)
                cpayload = self._payload(confirmed)
                self.assertEqual(cpayload, confirmed.structured_content)
                self.assertEqual(cpayload["status"], "ok")
                self.assertEqual({i["mp_id"] for i in cpayload["data"]["articles"]},
                                 {"feed-jwc"})
                self.assertTrue(all(i["source_status"] == "confirmed"
                                    for i in cpayload["data"]["articles"]))

                # 空 ID 列表必须是参数错误，不能扩成全订阅
                empty_ids = await client.call_tool(
                    "get_mp_articles", {"scope": "all", "days": 0, "mp_ids": []})
                self.assertTrue(empty_ids.is_error)
                epayload = self._payload(empty_ids)
                self.assertEqual(epayload["status"], "unavailable")
                self.assertIn("不能为空", epayload["reason"])
                self.assertNotIn("data", epayload)

            await self._run_client(base, scenario,
                                   extra_env={"ZJGSU_MP_RSS_DB": str(db),
                                              "ZJGSU_MP_SOURCES": str(registry)})

    async def test_missing_registry_refuses_school_confirmed_over_sdk(self):
        from test.test_mp_articles_scope import build_db

        with temp_dir() as td:
            base = Path(td)
            db = base / "mp.db"
            build_db(db)

            async def scenario(client):
                result = await client.call_tool(
                    "get_mp_articles", {"scope": "school_confirmed", "days": 0})
                self.assertTrue(result.is_error)
                payload = self._payload(result)
                self.assertEqual(payload, result.structured_content)
                self.assertEqual(payload["status"], "unavailable")
                self.assertIn("学校来源未解析", payload["reason"])
                self.assertFalse(payload["scope_info"]["registry"]["configured"])

            await self._run_client(base, scenario,
                                   extra_env={"ZJGSU_MP_RSS_DB": str(db),
                                              "ZJGSU_MP_SOURCES": str(base / "missing.json")})

    async def test_registry_follows_each_data_dir_and_explicit_override(self):
        """不同 profile 不能静默共享公众号的确认状态；显式路径仍可共享。"""
        from test.test_mp_articles_scope import build_db, write_registry

        with temp_dir() as td:
            root = Path(td)
            db = root / "synthetic.db"
            build_db(db)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            write_registry(first / "mp_sources.json", [
                {"id": "feed-jwc", "name": "示例来源一", "status": "confirmed"}])
            write_registry(second / "mp_sources.json", [
                {"id": "feed-cs", "name": "示例来源二", "status": "confirmed"}])

            async def selected(client):
                response = await client.call_tool(
                    "get_mp_articles", {"scope": "school_confirmed", "days": 0})
                self.assertFalse(response.is_error)
                body = self._payload(response)
                self.assertEqual(body, response.structured_content)
                return {article["mp_id"] for article in body["data"]["articles"]}

            default_env = {"ZJGSU_MP_RSS_DB": str(db), "ZJGSU_MP_SOURCES": ""}
            first_result = await self._run_client(first, selected, extra_env=default_env)
            second_result = await self._run_client(second, selected, extra_env=default_env)
            explicit_result = await self._run_client(
                second, selected, extra_env={"ZJGSU_MP_RSS_DB": str(db),
                    "ZJGSU_MP_SOURCES": str(first / "mp_sources.json")})
            self.assertEqual(first_result, {"feed-jwc"})
            self.assertEqual(second_result, {"feed-cs"})
            self.assertEqual(explicit_result, {"feed-jwc"})
