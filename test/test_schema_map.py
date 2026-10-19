# -*- coding: utf-8 -*-
"""schema 校验与映射测试（验收 ⑤：字段类型/日期/节次不合法时具名失败）。"""
from __future__ import annotations

import unittest

from test.helpers import course_item

from src.adapters.campus.schema import validate_and_map


class SchemaMapTest(unittest.TestCase):
    def test_valid_item_maps(self):
        entries, v = validate_and_map([course_item()])
        self.assertEqual(v, [])
        e = entries[0]
        self.assertEqual(e["entry_id"], "ID-1")
        self.assertEqual(e["course_name"], "课程A")
        self.assertEqual(e["weekday"], 1)
        self.assertEqual(e["period_start"], 1)
        self.assertEqual(e["period_end"], 2)
        self.assertEqual(e["week_no"], 5)
        self.assertEqual(e["date"], "2026-10-12")
        self.assertEqual(e["date_short"], "10-12")
        self.assertEqual(e["location"], "A101")
        self.assertEqual(e["source_id"], "src.course-api")

    def test_optional_missing_tolerated(self):
        it = course_item()
        for k in ("jsmc", "xn", "xq", "zc", "skrq", "kcdm", "kclx", "jsbh"):
            it.pop(k)
        entries, v = validate_and_map([it])
        self.assertEqual(v, [])
        e = entries[0]
        self.assertIsNone(e["location"])
        self.assertIsNone(e["academic_year"])
        self.assertIsNone(e["week_no"])
        self.assertIsNone(e["date_short"])

    def test_batch_rejected_on_first_violation(self):
        entries, v = validate_and_map([course_item(), course_item(xqj=9)])
        self.assertIsNone(entries)
        self.assertTrue(any("xqj" in x for x in v))

    def test_violations(self):
        cases = [
            ("缺 kcmc", course_item(kcmc=""), "kcmc"),
            ("xqj=9", course_item(xqj=9), "xqj"),
            ("xqj=True（布尔不入）", course_item(xqj=True), "xqj"),
            ("ksj=abc", course_item(ksj="abc"), "ksj"),
            ("jsj<ksj", course_item(ksj="6", jsj="3"), "jsj"),
            ("nyr 格式错", course_item(nyr="2026/10/12"), "nyr"),
            ("nyr 非法日期", course_item(nyr="2026-13-40"), "nyr"),
            ("id 缺失", course_item(id=""), "id"),
            ("skrq 格式错（存在时）", course_item(skrq="10/12"), "skrq"),
        ]
        for label, it, field in cases:
            entries, v = validate_and_map([it])
            self.assertIsNone(entries, label)
            self.assertTrue(any(field in x for x in v), "%s -> %s" % (label, v))

    def test_numeric_string_normalization(self):
        entries, v = validate_and_map([course_item(xqj="5", ksj="10", jsj="12", zc="7")])
        self.assertEqual(v, [])
        e = entries[0]
        self.assertEqual(e["weekday"], 5)
        self.assertEqual(e["period_start"], 10)
        self.assertEqual(e["week_no"], 7)


if __name__ == "__main__":
    unittest.main()
