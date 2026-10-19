# -*- coding: utf-8 -*-
"""宣讲会适配器单元测试：分类 / 映射 / 时间解析 / 状态推导（纯离线）。"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone, timedelta

from test.helpers import PROJECT_ROOT, event_item  # noqa: F401

from src.adapters.career.classify import classify_events
from src.adapters.career.events import SOURCE_JYW_XJH, derive_status, map_event

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 14, 15, 0, 0, tzinfo=TZ)  # 2026-10-14（周三）15:00


def _resp(items, total=None, pages=1, is_last=True, code=200):
    t = total if total is not None else len(items)
    return json.dumps({"code": code, "data": {
        "total": str(t), "list": items, "pages": pages, "isLastPage": is_last},
        "message": "操作成功"}, ensure_ascii=False)


class ClassifyEventsTest(unittest.TestCase):
    def test_ok(self):
        out, data, diag = classify_events("response", 200, _resp([event_item()]))
        self.assertEqual(out, "ok")
        self.assertEqual(data["total"], "1")

    def test_ok_empty(self):
        out, data, _ = classify_events("response", 200, _resp([], total=0, pages=0))
        self.assertEqual(out, "ok_empty")

    def test_business_500(self):
        out, _, diag = classify_events("response", 200, '{"code":500,"message":"操作异常！"}')
        self.assertEqual(out, "business_error")
        self.assertIn("500", diag)

    def test_business_403_not_mapped_to_auth(self):
        out, _, _ = classify_events("response", 200, '{"code":403,"message":"请登录"}')
        self.assertEqual(out, "business_error")

    def test_http_error(self):
        self.assertEqual(classify_events("response", 502, "")[0], "http_error")

    def test_network_error(self):
        self.assertEqual(classify_events("network_error", 0, "")[0], "network_error")

    def test_not_json(self):
        self.assertEqual(classify_events("response", 200, "<html>")[0], "parser_mismatch")

    def test_missing_code(self):
        self.assertEqual(classify_events("response", 200, '{"data":{}}')[0], "missing_code")

    def test_list_not_array(self):
        self.assertEqual(
            classify_events("response", 200, '{"code":200,"data":{"list":{},"total":"0","pages":0}}')[0],
            "parser_mismatch")

    def test_total_missing(self):
        self.assertEqual(
            classify_events("response", 200, '{"code":200,"data":{"list":[],"pages":0}}')[0],
            "parser_mismatch")

    def test_item_not_object(self):
        self.assertEqual(
            classify_events("response", 200, '{"code":200,"data":{"list":["x"],"total":"1","pages":1}}')[0],
            "parser_mismatch")

    # R14 反例：空列表与 total>0 矛盾——不得成为成功空结果
    def test_empty_list_but_total_positive(self):
        out, _, diag = classify_events("response", 200, _resp([], total=5, pages=1))
        self.assertEqual(out, "parser_mismatch")
        self.assertIn("矛盾", diag)


class MapEventTest(unittest.TestCase):
    def test_valid(self):
        e = map_event(event_item(), "2026-10-14")
        self.assertEqual(e["event_id"], "240000000000000001")
        self.assertEqual(e["company"], "示例公司")
        self.assertEqual(e["start_time"], "14:00")
        self.assertEqual(e["end_time"], "17:00")
        self.assertEqual(e["time_range"], "14:00-17:00")
        self.assertFalse(e["cancelled"])
        self.assertFalse(e["on_top"])
        self.assertEqual(e["source_id"], SOURCE_JYW_XJH)
        self.assertIn("xjhxx/view/240000000000000001", e["url"])

    def test_required_missing(self):
        self.assertIsNone(map_event(event_item(xjhid=""), "2026-10-14"))
        self.assertIsNone(map_event(event_item(xjhzt=""), "2026-10-14"))
        self.assertIsNone(map_event(event_item(dwmc=""), "2026-10-14"))
        self.assertIsNone(map_event(event_item(jbrq="2026/10/14"), "2026-10-14"))

    def test_date_mismatch_dropped(self):
        self.assertIsNone(map_event(event_item(jbrq="2026-10-15"), "2026-10-14"))

    def test_location_online(self):
        e = map_event(event_item(jbxs="02", cdmc=None, xsxjhwz="https://live.example.cn/x"), "2026-10-14")
        self.assertEqual(e["location"], "https://live.example.cn/x")

    def test_location_offline_uses_cdmc(self):
        e = map_event(event_item(jbxs="01", cdmc="C322", xsxjhwz="ignored"), "2026-10-14")
        self.assertEqual(e["location"], "C322")

    def test_cancelled_and_ontop(self):
        e = map_event(event_item(shzt="05", ontop="是"), "2026-10-14")
        self.assertTrue(e["cancelled"])
        self.assertTrue(e["on_top"])

    def test_time_variants(self):
        # R15：单时刻只确定开始，结束时间为 None（不猜）
        e = map_event(event_item(jbsjdmc="14:00"), "2026-10-14")
        self.assertEqual((e["start_time"], e["end_time"]), ("14:00", None))
        e2 = map_event(event_item(jbsjdmc="全天"), "2026-10-14")
        self.assertEqual((e2["start_time"], e2["end_time"]), (None, None))
        self.assertEqual(e2["time_range"], "全天")
        e3 = map_event(event_item(jbsjdmc=None), "2026-10-14")
        self.assertIsNone(e3["time_range"])


class DeriveStatusTest(unittest.TestCase):
    def _e(self, **over):
        return map_event(event_item(**over), "2026-10-14")

    def test_cancelled(self):
        self.assertEqual(derive_status(self._e(shzt="05"), NOW), "已取消")

    def test_upcoming(self):
        self.assertEqual(derive_status(self._e(jbsjdmc="18:00-20:00"), NOW), "未开始")

    def test_ongoing(self):
        self.assertEqual(derive_status(self._e(jbsjdmc="14:00-17:00"), NOW), "进行中")

    def test_ended(self):
        self.assertEqual(derive_status(self._e(jbsjdmc="08:00-09:00"), NOW), "已结束")

    def test_no_time(self):
        self.assertIsNone(derive_status(self._e(jbsjdmc=None), NOW))

    # R15：仅有开始时刻 → 「已开始（结束时间未知）」，绝不报「已结束」
    def test_single_time_unknown_end(self):
        e = self._e(jbsjdmc="14:00")
        self.assertEqual(derive_status(e, datetime(2026, 10, 14, 13, 0, tzinfo=TZ)), "未开始")
        self.assertEqual(derive_status(e, datetime(2026, 10, 14, 14, 30, tzinfo=TZ)),
                         "已开始（结束时间未知）")
        self.assertEqual(derive_status(e, datetime(2026, 10, 14, 23, 0, tzinfo=TZ)),
                         "已开始（结束时间未知）")

    # R15（2.13 残余）：跨日 23:00-01:00 结束日无法确定——不补猜，开始后未知
    def test_cross_midnight_unknown_end(self):
        e = self._e(jbsjdmc="23:00-01:00")
        self.assertEqual(derive_status(e, datetime(2026, 10, 14, 22, 0, tzinfo=TZ)), "未开始")
        self.assertEqual(derive_status(e, datetime(2026, 10, 14, 23, 30, tzinfo=TZ)),
                         "已开始（结束时间未知）")
        self.assertEqual(derive_status(e, datetime(2026, 10, 15, 2, 0, tzinfo=TZ)),
                         "已开始（结束时间未知）")


if __name__ == "__main__":
    unittest.main()
