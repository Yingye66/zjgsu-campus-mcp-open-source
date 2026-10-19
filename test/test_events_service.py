# -*- coding: utf-8 -*-
"""宣讲会服务层测试（合成数据、不触网）：逐日合并、分页读完、完整性、
空状态（今天无结果）、失败保留、游标、公共域（无需登录）、参数边界。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from test.helpers import (Clock, FakeTransport, event_item, events_resp, seed_session,
                          temp_dir)

from src.adapters.career.events import map_event
from src.domain.models import ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH
from src.service.campus_service import CampusService, _encode_cursor
from src.storage.store import SnapshotStore

D1, D2, D3 = "2026-10-14", "2026-10-15", "2026-10-16"
S12 = "%s~%s" % (D1, D2)


class EventsServiceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.tmp = Path(self._tmp.name)
        self.clock = Clock()  # 2026-10-13 14:00 (+08:00)
        self.transport = FakeTransport()
        self.store = SnapshotStore(self.tmp / "cache.sqlite3")
        self.svc = CampusService(self.store, self.tmp, transport=self.transport,
                                 now_fn=self.clock.now)

    def tearDown(self):
        self._tmp.cleanup()

    def calls(self):
        return self.transport.calls


class EventsFlowTests(EventsServiceBase):
    def test_today_empty_complete_and_public(self):
        # 无任何会话文件：公共来源仍可用
        self.transport.push(events_resp([], total=0, pages=0, is_last=True))
        r = self.svc.get_campus_events("2026-10-13", "2026-10-13")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["items"], [])
        snap = r["coverage"]["source_snapshots"][0]
        self.assertTrue(snap["complete"])
        self.assertEqual(r["issues"], [])  # complete=true → 无 completeness 说明
        # 请求 URL 与参数
        c = self.calls()[0]
        self.assertTrue(c["url"].endswith("/career/xjhxx/search/all/1/50"))
        self.assertEqual(c["form"]["jbrqs"], "2026-10-13")
        # 快照写入 public 域 + 空证据
        row = self.store.get_snapshot(ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH,
                                      "2026-10-13~2026-10-13")
        self.assertTrue(row["empty_evidence"])
        # 无会话时课表不可用、宣讲会可用（对比）
        self.assertEqual(self.svc.get_timetable(D1, D2)["issues"][0]["code"], "auth_required")

    def test_multi_day_merge_and_pages(self):
        # day1：2 页（2+1 条）；day2：0 条
        self.transport.push(events_resp(
            [event_item(xjhid="A1", dwmc="甲公司", jbsjdmc="09:00-10:00"),
             event_item(xjhid="A2", dwmc="乙公司", jbsjdmc="14:00-15:00")],
            total=3, pages=2, is_last=False, page_num=1))
        self.transport.push(events_resp(
            [event_item(xjhid="A3", dwmc="丙公司", jbsjdmc="18:00-19:00")],
            total=3, pages=2, is_last=True, page_num=2))
        self.transport.push(events_resp([], total=0, pages=0, is_last=True))
        r = self.svc.get_campus_events(D1, D2)
        self.assertEqual(r["status"], "ok")
        self.assertEqual([e["event_id"] for e in r["items"]], ["A1", "A2", "A3"])
        self.assertTrue(r["coverage"]["source_snapshots"][0]["complete"])
        self.assertEqual(r["issues"], [])
        urls = [c["url"] for c in self.calls()]
        self.assertEqual(urls, [
            "https://jyw.zjgsu.edu.cn/career/xjhxx/search/all/1/50",
            "https://jyw.zjgsu.edu.cn/career/xjhxx/search/all/2/50",
            "https://jyw.zjgsu.edu.cn/career/xjhxx/search/all/1/50",
        ])
        self.assertEqual(self.calls()[1]["form"]["jbrqs"], D1)
        self.assertEqual(self.calls()[2]["form"]["jbrqs"], D2)

    def test_status_computed_at_serve_time(self):
        # 缓存写入后时间推进 → 状态随组装时间变化（未开始 → 进行中）
        # 时钟起点 2026-10-13 14:00；事件在 2026-10-14 15:00-16:00 → 推进 25 小时到事件开始时刻
        self.transport.push(events_resp([event_item(jbsjdmc="15:00-16:00")]))
        r1 = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r1["items"][0]["status"], "未开始")
        self.clock.advance(3600 * 25)  # now = 2026-10-14 15:00
        sid = r1["coverage"]["source_snapshots"][0]["snapshot_id"]
        cur = _encode_cursor(ACCOUNT_PUBLIC, MODULE_EVENTS, D1 + "~" + D1, sid, 0)
        r2 = self.svc.get_campus_events(D1, D1, cursor=cur)
        self.assertEqual(r2["items"][0]["status"], "进行中")  # 从缓存重算

    def test_truncated_pages_marked(self):
        # pages=9 > 上限 5 → 读 5 页后停止，complete=False + missing_ranges + 说明
        # 契约（交接 2.11）：限页截断 → partial（与第 8 节一致）
        for p in range(1, 6):
            self.transport.push(events_resp(
                [event_item(xjhid="P%d" % p)], total=9, pages=9,
                is_last=False, page_num=p))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "partial")
        self.assertEqual(len(r["items"]), 5)
        snap = r["coverage"]["source_snapshots"][0]
        self.assertFalse(snap["complete"])
        self.assertTrue(snap["missing_ranges"])
        self.assertIn("completeness_unproven", [i["code"] for i in r["issues"]])
        self.assertEqual(len(self.calls()), 5)  # 有界：不超过 MAX_PAGES_PER_DAY

    def test_failure_keeps_old_and_reports(self):
        self.transport.push(events_resp([event_item(xjhid="K1")]))
        r1 = self.svc.get_campus_events(D1, D1)
        t1 = r1["freshness"]["last_success_at"]
        sid1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]

        self.clock.advance(60)
        self.transport.push({"kind": "network_error", "err": "timeout"})
        r2 = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r2["status"], "partial")
        self.assertEqual(r2["items"][0]["event_id"], "K1")
        self.assertEqual(r2["freshness"]["last_success_at"], t1)
        self.assertIn("network_error", [i["code"] for i in r2["issues"]])
        row = self.store.get_snapshot(ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH, D1 + "~" + D1)
        self.assertEqual(row["snapshot_id"], sid1)

        # 业务失败（code=500）同样保留旧数据
        self.clock.advance(60)
        self.transport.push(events_resp([], code=500))
        r3 = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r3["status"], "partial")
        self.assertIn("business_error", [i["code"] for i in r3["issues"]])

    def test_keyword_scope_isolation(self):
        self.transport.push(events_resp([event_item(xjhid="W1")]))
        r1 = self.svc.get_campus_events(D1, D1, keyword="宁波银行")
        self.assertEqual(r1["items"][0]["event_id"], "W1")
        self.assertEqual(self.calls()[0]["form"]["dwmc"], "宁波银行")
        # 另一关键词 → 独立快照（不读上一个）
        self.transport.push(events_resp([], total=0, pages=0))
        r2 = self.svc.get_campus_events(D1, D1, keyword="招商银行")
        self.assertEqual(r2["items"], [])
        self.assertTrue(r2["coverage"]["source_snapshots"][0]["complete"])
        scope_kw1 = [s for s in ["%s~%s|kw=宁波银行" % (D1, D1)]][0]
        self.assertIsNotNone(self.store.get_snapshot(ACCOUNT_PUBLIC, MODULE_EVENTS,
                                                     SOURCE_JYW_XJH, scope_kw1))
        # 无关键词与有关键词互不影响
        self.transport.push(events_resp([event_item(xjhid="W2")]))
        r3 = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r3["items"][0]["event_id"], "W2")

    def test_cursor_paging_and_expiry(self):
        items = [event_item(xjhid="C%d" % i) for i in range(3)]
        self.transport.push(events_resp(items, total=3, pages=1, is_last=True))
        r1 = self.svc.get_campus_events(D1, D1, limit=2)
        self.assertEqual(len(r1["items"]), 2)
        self.assertTrue(r1["pagination"]["has_more"])
        cur = r1["pagination"]["next_cursor"]
        n_calls = len(self.calls())

        r2 = self.svc.get_campus_events(D1, D1, cursor=cur, limit=2)
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(len(r2["items"]), 1)
        self.assertEqual(len(self.calls()), n_calls)  # 游标路径不触网

        # 刷新后旧游标失效
        self.clock.advance(60)
        self.transport.push(events_resp([event_item(xjhid="NEW")]))
        self.svc.get_campus_events(D1, D1)
        r3 = self.svc.get_campus_events(D1, D1, cursor=cur)
        self.assertEqual(r3["status"], "unavailable")
        self.assertEqual(r3["issues"][0]["code"], "snapshot_expired")

    def test_cursor_wrong_module_rejected(self):
        # 用课表模块构造的游标在 events 上必须被拒
        bad = _encode_cursor("public", "campus.timetable", "x", "sid", 0)
        r = self.svc.get_campus_events(D1, D1, cursor=bad)
        self.assertEqual(r["issues"][0]["code"], "snapshot_expired")

    def test_argument_limits(self):
        r = self.svc.get_campus_events(D1, D1, event_type="招聘会")
        self.assertEqual(r["issues"][0]["code"], "unsupported")
        r2 = self.svc.get_campus_events("2026-10-01", "2026-10-20")  # 20 天 > 14
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")
        r3 = self.svc.get_campus_events("2026-13-01", "2026-13-02")
        self.assertEqual(r3["issues"][0]["code"], "invalid_arguments")
        r4 = self.svc.get_campus_events(D2, D1)
        self.assertEqual(r4["issues"][0]["code"], "invalid_arguments")
        r5 = self.svc.get_campus_events(D1, D1, limit=0)
        self.assertEqual(r5["issues"][0]["code"], "invalid_arguments")
        self.assertEqual(len(self.calls()), 0)  # 参数错误不触网

    def test_health_includes_public_source(self):
        self.transport.push(events_resp([event_item()]))
        self.svc.get_campus_events(D1, D1)
        h = self.svc.get_source_health()
        rows = {r["source_id"]: r for r in h["sources"]}
        self.assertIn(SOURCE_JYW_XJH, rows)
        self.assertEqual(rows[SOURCE_JYW_XJH]["account_scope_id"], ACCOUNT_PUBLIC)
        self.assertIsNotNone(rows[SOURCE_JYW_XJH]["last_success_at"])
        # 会话种子存在时课表行也出现
        seed_session(self.tmp, "acct-dd00000000000004")
        h2 = self.svc.get_source_health("campus.events")
        self.assertTrue(all(r["module_id"] == "campus.events" for r in h2["sources"]))


class AdvancingTransport(FakeTransport):
    """每次请求后推进时钟——模拟「取数完成晚于请求发起」（R16）。"""

    def __init__(self, clock, advance_seconds: int):
        super().__init__()
        self._clock = clock
        self._advance = advance_seconds

    def post_form(self, url, form=None, headers=None, timeout=20):
        self._clock.advance(self._advance)
        return super().post_form(url, form=form, headers=headers, timeout=timeout)


class EventsFeedbackR14_R17Tests(EventsServiceBase):
    """交接 2.11 反馈 R14–R17 的回归用例。"""

    # ---- R14：分页元数据矛盾不成为成功（空）快照 ----
    def test_r14_empty_list_total_positive_no_snapshot(self):
        self.transport.push(events_resp([], total=5, pages=1, is_last=True))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])
        self.assertIsNone(self.store.get_snapshot(ACCOUNT_PUBLIC, MODULE_EVENTS,
                                                  SOURCE_JYW_XJH, D1 + "~" + D1))

    def test_r14_abnormal_empty_keeps_old_snapshot(self):
        # 先有成功快照 → 异常空响应：旧数据与 last_success_at 保留，返回具名失败
        self.transport.push(events_resp([event_item(xjhid="OK1")]))
        r1 = self.svc.get_campus_events(D1, D1)
        sid1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]
        t1 = r1["freshness"]["last_success_at"]
        self.clock.advance(60)
        self.transport.push(events_resp([], total=5, pages=1, is_last=True))
        r2 = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r2["status"], "partial")
        self.assertEqual(r2["items"][0]["event_id"], "OK1")
        self.assertEqual(r2["freshness"]["last_success_at"], t1)
        self.assertIn("parser_mismatch", [i["code"] for i in r2["issues"]])
        row = self.store.get_snapshot(ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH,
                                      D1 + "~" + D1)
        self.assertEqual(row["snapshot_id"], sid1)

    def test_r14_last_false_string_not_read_as_true(self):
        # isLastPage="false"（字符串）：不得按真值折叠；total>已读 → 矛盾，不标完整
        self.transport.push(events_resp([event_item(xjhid="L1")], total=2, pages=1,
                                        is_last="false"))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])

    def test_r14_duplicate_ids_across_pages(self):
        self.transport.push(events_resp([event_item(xjhid="D1x")], total=2, pages=2,
                                        is_last=False, page_num=1))
        self.transport.push(events_resp([event_item(xjhid="D1x")], total=2, pages=2,
                                        is_last=True, page_num=2))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])

    def test_r14_page_num_mismatch(self):
        self.transport.push(events_resp([event_item(xjhid="N1")], total=2, pages=2,
                                        is_last=False, page_num=1))
        # 第 2 页请求却返回 pageNum=1
        self.transport.push(events_resp([event_item(xjhid="N2")], total=2, pages=2,
                                        is_last=True, page_num=1))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])

    def test_r14_total_exceeds_pages_times_size(self):
        self.transport.push(events_resp([event_item(xjhid="T1")], total=100, pages=1,
                                        is_last=True))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])

    # ---- 多日部分失败：保留已读部分，缺口具名 ----
    def test_multi_day_partial_failure_keeps_read_days(self):
        self.transport.push(events_resp([event_item(xjhid="DAY1")]))
        self.transport.push({"kind": "network_error", "err": "timeout"})
        r = self.svc.get_campus_events(D1, D2)
        self.assertEqual(r["status"], "partial")
        self.assertEqual([e["event_id"] for e in r["items"]], ["DAY1"])
        codes = [i["code"] for i in r["issues"]]
        self.assertIn("network_error", codes)
        self.assertIn("completeness_unproven", codes)
        snap = r["coverage"]["source_snapshots"][0]
        self.assertFalse(snap["complete"])
        self.assertTrue(snap["missing_ranges"])

    # ---- R16：状态与新鲜度按取数完成时刻 ----
    def test_r16_status_and_freshness_use_completion_time(self):
        adv = AdvancingTransport(self.clock, 3600 * 24 + 60)
        self.svc = CampusService(self.store, self.tmp, transport=adv, now_fn=self.clock.now)
        # 事件 14:01-15:00（D1=2026-10-14）；请求发起于 10-13 14:00，
        # 取数完成于 10-14 14:01（时钟在请求中推进）→ 应为「进行中」而非「未开始」
        adv.push(events_resp([event_item(jbsjdmc="14:01-15:00")]))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["items"][0]["status"], "进行中")
        self.assertEqual(r["freshness"]["last_success_at"], "2026-10-14T14:01:00+08:00")
        self.assertEqual(r["freshness"]["last_attempt_at"], "2026-10-14T14:01:00+08:00")

    # ---- R17：严格 yyyy-MM-dd（非补零拒绝，且发生在触网前） ----
    def test_r17_non_padded_dates_rejected(self):
        r = self.svc.get_campus_events("2026-1-1", "2026-1-2")
        self.assertEqual(r["issues"][0]["code"], "invalid_arguments")
        r2 = self.svc.get_campus_events(20260101, 20260102)  # 非字符串
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")
        r3 = self.svc.get_timetable("2026-1-1", "2026-1-2")
        self.assertEqual(r3["issues"][0]["code"], "invalid_arguments")
        r4 = self.svc.get_campus_events("2026-02-30", "2026-02-30")  # 非法日
        self.assertEqual(r4["issues"][0]["code"], "invalid_arguments")
        self.assertEqual(len(self.calls()), 0)  # 全部在触网前拒绝


class EventsSecondRecheckTests(EventsServiceBase):
    """交接 2.13 第二轮复验反馈的针对性回归。"""

    @staticmethod
    def _raw_resp(data: dict) -> dict:
        return {"kind": "response", "status": 200,
                "text": json.dumps({"code": 200, "data": data, "message": "操作成功"},
                                   ensure_ascii=False)}

    # ---- R14 残余：结束页核对累计条数 vs total ----
    def test_final_page_count_vs_total_rejected(self):
        self.transport.push(events_resp([event_item(xjhid="A")], total=5, pages=1,
                                        is_last=True))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])

    def test_same_page_duplicate_ids_rejected(self):
        self.transport.push(events_resp(
            [event_item(xjhid="A"), event_item(xjhid="A", dwmc="另一家")],
            total=2, pages=1, is_last=True))
        r = self.svc.get_campus_events(D1, D1)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])

    # ---- R14 页码类型：非 int / bool / 缺失 一律拒绝 ----
    def test_page_num_type_rejected(self):
        base = {"total": "1", "list": [event_item()], "pageSize": 50, "pages": 1,
                "isLastPage": True}
        self.transport.push(self._raw_resp({**base, "pageNum": "1"}))   # 字符串
        r = self.svc.get_campus_events(D1, D1)
        self.assertIn("parser_mismatch", [i["code"] for i in r["issues"]])
        self.transport.push(self._raw_resp({**base, "pageNum": True}))  # bool
        r2 = self.svc.get_campus_events(D1, D1)
        self.assertIn("parser_mismatch", [i["code"] for i in r2["issues"]])
        self.transport.push(self._raw_resp(base))                        # 缺失
        r3 = self.svc.get_campus_events(D1, D1)
        self.assertIn("parser_mismatch", [i["code"] for i in r3["issues"]])

    # ---- R15 残余：旧缓存（补猜 end_time）从 time_range 重解析 ----
    def test_old_cache_end_time_reparse_on_cursor(self):
        entry = map_event(event_item(jbsjdmc="14:00", jbrq=D1), D1)
        entry["time_range"] = "14:00"
        entry["end_time"] = "14:00"  # 模拟旧解析器补猜
        snap = self.store.upsert_snapshot(
            ACCOUNT_PUBLIC, MODULE_EVENTS, SOURCE_JYW_XJH, D1 + "~" + D1,
            items=[entry], fetched_at="2026-10-13T10:00:00+08:00",
            complete=True, consistency="best_effort", missing_ranges=[],
            empty_evidence=False)
        self.clock.advance(3600 * 24 + 120)  # 10-14 14:02
        cur = _encode_cursor(ACCOUNT_PUBLIC, MODULE_EVENTS, D1 + "~" + D1,
                             snap["snapshot_id"], 0)
        r = self.svc.get_campus_events(D1, D1, cursor=cur)
        # 旧实现会按 end_time=14:00 报「已结束」；重解析后为未知结束
        self.assertEqual(r["items"][0]["status"], "已开始（结束时间未知）")

    # ---- 游标 partial 语义 ----
    def test_cursor_partial_for_incomplete_events(self):
        for p in range(1, 6):
            self.transport.push(events_resp([event_item(xjhid="P%d" % p)],
                                            total=9, pages=9, is_last=False,
                                            page_num=p))
        r1 = self.svc.get_campus_events(D1, D1, limit=2)
        self.assertEqual(r1["status"], "partial")
        r2 = self.svc.get_campus_events(D1, D1, cursor=r1["pagination"]["next_cursor"])
        self.assertEqual(r2["status"], "partial")
        self.assertFalse(r2["coverage"]["source_snapshots"][0]["complete"])
        self.assertIn("completeness_unproven", [i["code"] for i in r2["issues"]])

    # ---- 多日缺口：失败日 + 后续未尝试日 ----
    def test_three_day_gap_includes_untried(self):
        self.transport.push(events_resp([event_item(xjhid="DAY1")]))
        self.transport.push({"kind": "network_error", "err": "timeout"})
        r = self.svc.get_campus_events(D1, D3)
        self.assertEqual(r["status"], "partial")
        missing = " ".join(r["coverage"]["source_snapshots"][0]["missing_ranges"])
        self.assertIn(D2, missing)
        self.assertIn(D3, missing)
        self.assertIn("未尝试", missing)

    # ---- 多日缺口：此前截断 + 失败 ----
    def test_gap_includes_prior_truncation_then_failure(self):
        for p in range(1, 6):
            self.transport.push(events_resp([event_item(xjhid="P%d" % p)],
                                            total=9, pages=9, is_last=False,
                                            page_num=p))
        self.transport.push({"kind": "network_error", "err": "timeout"})
        r = self.svc.get_campus_events(D1, D2)
        self.assertEqual(r["status"], "partial")
        missing = " ".join(r["coverage"]["source_snapshots"][0]["missing_ranges"])
        self.assertIn("仅读前 5 页", missing)
        self.assertIn(D2, missing)


if __name__ == "__main__":
    unittest.main()
