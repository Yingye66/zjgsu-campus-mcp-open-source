# -*- coding: utf-8 -*-
"""薄适配器最小验收（交接文档第 2.6 节五条）+ cursor/认证路径。

① 两个账号/两个日期范围互不读取各自缓存；
② 成功快照后模拟未知码/HTTP/解析失败：旧数据与 last_success_at 不变，
   last_attempt_at/issue 更新；
③ 已知成功码空数组可替换该范围结果；未知码空数组不可；
④ 完整性待证时 complete=false 并给原因；
⑤ 字段类型/日期/节次不合法时具名失败（不以「对象数组」代替业务 schema）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from test.helpers import (Clock, FakeTransport, course_code, course_item, course_ok,
                          http_500, network_err, not_json, seed_session, temp_dir)

from src.domain.models import MODULE_TIMETABLE, SOURCE_COURSE_API
from src.service.campus_service import CampusService, _encode_cursor
from src.storage.store import SnapshotStore

ACCT_A = "acct-aaaa000000000001"
ACCT_B = "acct-bbbb000000000002"
R1 = ("2026-10-12", "2026-10-18")
R2 = ("2026-10-19", "2026-10-25")
S1 = "%s~%s" % R1
S2 = "%s~%s" % R2


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.tmp = Path(self._tmp.name)
        self.clock = Clock()
        self.transport = FakeTransport()
        self.store = SnapshotStore(self.tmp / "cache.sqlite3")
        self.svc = CampusService(self.store, self.tmp, transport=self.transport,
                                 now_fn=self.clock.now)

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, rng=R1, **kw):
        return self.svc.get_timetable(rng[0], rng[1], **kw)


class Acceptance123Tests(ServiceBase):
    def test_01_isolation_two_accounts_two_ranges(self):
        # A 登入并成功写入 R1
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item(id="A1")]))
        ra = self.call(R1)
        self.assertEqual(ra["status"], "ok")
        self.assertEqual(ra["items"][0]["entry_id"], "A1")

        # 切到 B：同一范围返回 B 自己的数据（不读 A 的缓存）
        seed_session(self.tmp, ACCT_B)
        self.transport.push(course_ok([course_item(id="B1")]))
        rb = self.call(R1)
        self.assertEqual(rb["status"], "ok")
        self.assertEqual(rb["items"][0]["entry_id"], "B1")
        self.assertNotEqual(rb["items"][0]["entry_id"], "A1")

        # 存储层直接核验隔离
        sa = self.store.get_snapshot(ACCT_A, MODULE_TIMETABLE, SOURCE_COURSE_API, S1)
        sb = self.store.get_snapshot(ACCT_B, MODULE_TIMETABLE, SOURCE_COURSE_API, S1)
        self.assertEqual(sa["items"][0]["entry_id"], "A1")
        self.assertEqual(sb["items"][0]["entry_id"], "B1")

        # A 查询另一个范围 R2 且失败 → 不得读取 R1 或 B 的缓存 → unavailable
        seed_session(self.tmp, ACCT_A)
        self.transport.push(network_err())
        r2 = self.call(R2)
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["items"], [])

    def test_02_failure_keeps_old_data_and_last_success(self):
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item(id=f"D{i}") for i in range(3)]))
        r1 = self.call(R1)
        self.assertEqual(r1["status"], "ok")
        t1 = r1["freshness"]["last_success_at"]
        sid1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]

        # 未知业务码（含非空数组）→ partial，旧数据与最后成功时间不变
        self.clock.advance(60)
        self.transport.push(course_code("500", [course_item(id="X1")]))
        r2 = self.call(R1)
        self.assertEqual(r2["status"], "partial")
        self.assertEqual(len(r2["items"]), 3)
        self.assertEqual(r2["freshness"]["last_success_at"], t1)
        self.assertEqual(r2["freshness"]["last_attempt_at"],
                         self.clock.now().isoformat(timespec="seconds"))
        self.assertIn("unknown_code", [i["code"] for i in r2["issues"]])
        snap = self.store.get_snapshot(ACCT_A, MODULE_TIMETABLE, SOURCE_COURSE_API, S1)
        self.assertEqual(snap["snapshot_id"], sid1)

        # HTTP 失败 → 同上
        self.clock.advance(60)
        self.transport.push(http_500())
        r3 = self.call(R1)
        self.assertEqual(r3["status"], "partial")
        self.assertEqual(len(r3["items"]), 3)
        self.assertEqual(r3["freshness"]["last_success_at"], t1)
        self.assertIn("http_error", [i["code"] for i in r3["issues"]])

        # 解析失败 → 同上
        self.clock.advance(60)
        self.transport.push(not_json())
        r4 = self.call(R1)
        self.assertEqual(r4["status"], "partial")
        self.assertEqual(r4["freshness"]["last_success_at"], t1)
        self.assertIn("parser_mismatch", [i["code"] for i in r4["issues"]])

        # 健康记录：last_success 仍是 t1，last_issue 为最近一次
        h = self.store.get_health_rows(ACCT_A, MODULE_TIMETABLE)
        row = [x for x in h if x["source_id"] == SOURCE_COURSE_API][0]
        self.assertEqual(row["last_success_at"], t1)
        self.assertEqual(row["last_issue"]["code"], "parser_mismatch")

    def test_03_empty_array_semantics(self):
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item(id="K1")]))
        r1 = self.call(R1)
        sid1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]
        t1 = r1["freshness"]["last_success_at"]

        # 未知码 + 空数组：不可替换（旧数据保留）
        self.clock.advance(60)
        self.transport.push(course_code("500", []))
        r2 = self.call(R1)
        self.assertEqual(r2["status"], "partial")
        self.assertEqual(len(r2["items"]), 1)
        self.assertEqual(r2["freshness"]["last_success_at"], t1)
        snap = self.store.get_snapshot(ACCT_A, MODULE_TIMETABLE, SOURCE_COURSE_API, S1)
        self.assertEqual(snap["snapshot_id"], sid1)

        # 已知成功码 0 + 空数组：可替换（建立合法空快照）
        self.clock.advance(60)
        self.transport.push(course_code("0", []))
        r3 = self.call(R1)
        self.assertEqual(r3["status"], "ok")
        self.assertEqual(r3["items"], [])
        self.assertNotEqual(r3["freshness"]["last_success_at"], t1)
        snap2 = self.store.get_snapshot(ACCT_A, MODULE_TIMETABLE, SOURCE_COURSE_API, S1)
        self.assertNotEqual(snap2["snapshot_id"], sid1)
        self.assertTrue(snap2["empty_evidence"])

    def test_04_completeness_unproven(self):
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item()]))
        r = self.call(R1)
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["coverage"]["source_snapshots"][0]["complete"])
        codes = [i["code"] for i in r["issues"]]
        self.assertIn("completeness_unproven", codes)
        msg = [i["safe_message"] for i in r["issues"]
               if i["code"] == "completeness_unproven"][0]
        self.assertIn("1 条", msg)

    def test_05_schema_invalid_named_failure(self):
        # 无旧快照：unavailable + parser_mismatch（不建立快照）
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item(xqj=9)]))
        r1 = self.call(R1)
        self.assertEqual(r1["status"], "unavailable")
        codes = [i["code"] for i in r1["issues"]]
        self.assertIn("parser_mismatch", codes)
        self.assertTrue(any("xqj" in i["safe_message"] for i in r1["issues"]))
        self.assertIsNone(self.store.get_snapshot(ACCT_A, MODULE_TIMETABLE, SOURCE_COURSE_API, S1))

        # 有旧快照：partial（旧数据保留，last_success 不变）
        self.transport.push(course_ok([course_item(id="OK1")]))
        r2 = self.call(R1)
        self.assertEqual(r2["status"], "ok")
        t2 = r2["freshness"]["last_success_at"]
        self.clock.advance(60)
        self.transport.push(course_ok([course_item(ksj="abc")]))
        r3 = self.call(R1)
        self.assertEqual(r3["status"], "partial")
        self.assertEqual(r3["freshness"]["last_success_at"], t2)
        self.assertIn("parser_mismatch", [i["code"] for i in r3["issues"]])


class CursorAndAuthTests(ServiceBase):
    def test_cursor_paging_without_refetch(self):
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item(id=f"P{i}") for i in range(3)]))
        r1 = self.call(R1, limit=2)
        self.assertEqual([x["entry_id"] for x in r1["items"]], ["P0", "P1"])
        self.assertTrue(r1["pagination"]["has_more"])
        cur = r1["pagination"]["next_cursor"]
        calls_before = len(self.transport.calls)

        r2 = self.call(R1, cursor=cur, limit=2)
        self.assertEqual(r2["status"], "ok")
        self.assertEqual([x["entry_id"] for x in r2["items"]], ["P2"])
        self.assertFalse(r2["pagination"]["has_more"])
        self.assertEqual(len(self.transport.calls), calls_before)  # 未触发上游请求

    def test_cursor_expired_after_refresh(self):
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item(id=f"Q{i}") for i in range(3)]))
        r1 = self.call(R1, limit=2)
        cur = r1["pagination"]["next_cursor"]

        self.clock.advance(60)
        self.transport.push(course_ok([course_item(id="NEW")]))
        self.call(R1)  # 刷新 → 快照替换

        r3 = self.call(R1, cursor=cur, limit=2)
        self.assertEqual(r3["status"], "unavailable")
        self.assertEqual(r3["issues"][0]["code"], "snapshot_expired")

    def test_cursor_rejected_for_other_account(self):
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item(id=f"Z{i}") for i in range(3)]))
        r1 = self.call(R1, limit=2)
        cur = r1["pagination"]["next_cursor"]

        seed_session(self.tmp, ACCT_B)  # 切到 B
        r2 = self.call(R1, cursor=cur, limit=2)
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["issues"][0]["code"], "snapshot_expired")

    def test_cursor_garbage_rejected(self):
        seed_session(self.tmp, ACCT_A)
        r = self.call(R1, cursor="not-a-cursor!!")
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "snapshot_expired")

    def test_auth_required_without_session(self):
        r = self.call(R1)
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "auth_required")
        self.assertEqual(len(self.transport.calls), 0)  # 未触网

    def test_non_student_unsupported(self):
        seed_session(self.tmp, ACCT_A, user_type="1")
        r = self.call(R1)
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "unsupported")
        self.assertEqual(len(self.transport.calls), 0)

    def test_invalid_arguments(self):
        seed_session(self.tmp, ACCT_A)
        r = self.call(("2026-13-99", "2026-14-01"))
        self.assertEqual(r["issues"][0]["code"], "invalid_arguments")
        r2 = self.call(("2026-10-19", "2026-10-12"))
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")
        r3 = self.call(R1, limit=0)
        self.assertEqual(r3["issues"][0]["code"], "invalid_arguments")
        self.assertEqual(len(self.transport.calls), 0)

    def test_health_report(self):
        seed_session(self.tmp, ACCT_A)
        self.transport.push(course_ok([course_item()]))
        self.call(R1)
        h = self.svc.get_source_health()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["session"]["present"])
        self.assertEqual(h["session"]["user_type"], "2")
        row = h["sources"][0]
        self.assertEqual(row["source_id"], SOURCE_COURSE_API)
        self.assertIsNotNone(row["last_success_at"])
        self.assertIsNone(row["last_issue_code"])
        # 不含秘密
        self.assertNotIn("TEST_TOKEN_%s" % ACCT_A, str(h))

    def test_capabilities_report(self):
        c = self.svc.list_capabilities()
        self.assertEqual(c["status"], "ok")
        ids = [m["module_id"] for m in c["modules"]]
        self.assertIn("campus.timetable", ids)
        tt = [m for m in c["modules"] if m["module_id"] == "campus.timetable"][0]
        self.assertIn("get_timetable", tt["tools"])
        self.assertIn("coverage_unknown", c["coverage_note"])


if __name__ == "__main__":
    unittest.main()
