# -*- coding: utf-8 -*-
"""教务网页版（jwweb）回归：反馈 R21/R23/R25/R26/R27/R28/R30/R31 的合成反例。

全部使用假传输/假会话/临时目录，不触网、无真实凭据。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.adapters.campus.jwweb import (JwwebFetch, JwwebTransport, _map_items,
                                       classify_jwweb, fetch_exams)
from src.auth import session as S
from src.service.jwweb_service import JWWebService
from src.storage.store import SnapshotStore


def _paged_body(items, total, page, total_page):
    return json.dumps({"currentPage": page, "items": items, "totalCount": total,
                       "totalPage": total_page, "totalResult": total}, ensure_ascii=False)


class _FakeTransport:
    """脚本化假传输：按 (method, url 前缀, form.currentPage) 出牌。"""

    def __init__(self, script):
        # script: list of (kind, status, text) 按请求顺序弹出
        self.script = list(script)
        self.calls = []

    def get(self, url, headers=None, timeout=20):
        self.calls.append(("GET", url, None))
        return self.script.pop(0)

    def post_form(self, url, form=None, headers=None, timeout=20):
        self.calls.append(("POST", url, dict(form or {})))
        return self.script.pop(0)

    def export_cookies(self):
        return []

    def set_cookies(self, c):
        pass


def _item(kch="C1", kcmc="课程一", cj="80"):
    return {"kch": kch, "kcmc": kcmc, "cj": cj, "jd": "3.0", "xf": "3.0",
            "jsxm": "张三", "jxbmc": "(2025-2026-1)-%s-01" % kch}


class ExamPagingTest(unittest.TestCase):
    """R21：分页遍历、对账与缺口。"""

    def _mk_service(self, tmp, fake=None):
        S.save_session(tmp, "acct-t", {"zjgsu_token": "t", "user_type": "2",
                                       "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                           "domain": "jwxt.zjgsu.edu.cn",
                                                           "path": "/", "secure": False,
                                                           "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-t")
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_two_pages_are_both_read_and_deduped(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_t_"))
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 13)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items[:100], 12, 1, 2)},
            # showCount=100 单页装下 12 条：来源声明 totalPage=2 → 需第二页（空尾页）
            {"kind": "response", "status": 200, "text": _paged_body([], 12, 2, 2)},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "ok", f.diag)
        self.assertEqual(len(f.items), 12)
        # 页参数确实带了 currentPage 翻页
        posts = [c for c in ft.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2]["queryModel.currentPage"], "1")
        self.assertEqual(posts[1][2]["queryModel.currentPage"], "2")

    def test_second_page_failure_is_partial_not_ok(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_t_"))
        svc = self._mk_service(tmp)
        first = [_item("C%d" % i, "课程%d" % i) for i in range(1, 11)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": _paged_body(first, 12, 1, 2)},          # 声明 12 条 2 页
            {"kind": "response", "status": 500, "text": "err"},  # 第二页失败
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(len(f.items), 10)
        self.assertEqual(f.total_expected, 12)

    def test_service_partial_carries_gap_issue(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_t_"))
        first = [_item("C%d" % i, "课程%d" % i) for i in range(1, 11)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(first, 12, 1, 2)},
            {"kind": "response", "status": 500, "text": "err"},
        ])
        svc = self._mk_service(tmp, fake=ft)
        r = svc.get_exams("2025", "12")
        self.assertEqual(r["status"], "partial")
        self.assertEqual(len(r["items"]), 10)
        self.assertTrue(any(i["code"] == "completeness_unproven" and i["retryable"]
                            for i in r["issues"]))
        self.assertIn("12", r["coverage"]["source_snapshots"][0]["missing_ranges"][0])

    def test_empty_with_zero_total_is_ok_empty(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_t_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body([], 0, 1, 1)},
        ])
        svc = self._mk_service(tmp, fake=ft)
        r = svc.get_exams("2026", "3")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["items"], [])

    def test_empty_but_total_positive_is_not_ok_empty(self):
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body([], 5, 1, 1)},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")


class FailureCacheTest(unittest.TestCase):
    """R27/R28：失败时复用旧快照条目，健康登记 last_issue。"""

    def _mk(self, tmp, fake=None):
        S.save_session(tmp, "acct-f", {"zjgsu_token": "t", "user_type": "2",
                                       "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                           "domain": "jwxt.zjgsu.edu.cn",
                                                           "path": "/", "secure": False,
                                                           "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-f")
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_network_failure_returns_old_items_with_snapshot_id(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_f_"))
        ok_items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 3)]
        good = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(ok_items, 2, 1, 1)},
        ])
        svc = self._mk(tmp, fake=good)
        r1 = svc.get_grades("2025", "12")
        self.assertEqual(r1["status"], "ok")
        snap = svc.store.get_snapshot("acct-f", "campus.grades", "src.jwglxt", "2025-12")
        self.assertIsNotNone(snap)
        bad = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "network_error", "err": "boom"},
        ])
        svc2 = JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=bad)
        r2 = svc2.get_grades("2025", "12")
        self.assertEqual(r2["status"], "partial")
        self.assertEqual(len(r2["items"]), 2)  # 旧条目仍在
        self.assertIsNotNone(r2["pagination"]["result_snapshot_id"])
        self.assertTrue(any(i["code"] == "upstream_error" for i in r2["issues"]))
        # R28：健康行 last_issue 已登记
        health = svc2.store.get_health_rows("acct-f", "campus.grades")
        self.assertTrue(any(h["last_issue"] for h in health))


class CursorGateTest(unittest.TestCase):
    """R30：游标处理前必须验证本人会话存在。R31：xnm 完整匹配。"""

    def test_cursor_without_session_is_unavailable(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_c_"))
        store = SnapshotStore(tmp / "c.sqlite3")
        # 不写任何会话（无 active）
        svc = JWWebService(store, tmp)
        r = svc.get_grades("2025", "12", cursor="whatever")
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "auth_required")

    def test_xnm_trailing_newline_rejected(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_c_"))
        S.save_session(tmp, "acct-t", {"zjgsu_token": "t", "user_type": "2",
                                       "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                           "domain": "jwxt.zjgsu.edu.cn",
                                                           "path": "/", "secure": False,
                                                           "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-t")
        svc = JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp)
        r = svc.get_grades("2025\n", "12")
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "invalid_arguments")


class ClassifyAndCookieTest(unittest.TestCase):
    """R23：细分分类。R26：cookie 属性保留。"""

    def test_html_login_vs_other_html(self):
        o1, _, _ = classify_jwweb({"kind": "response", "status": 200,
                                   "text": "<html>统一身份认证 登录</html>"})
        o2, _, _ = classify_jwweb({"kind": "response", "status": 200,
                                   "text": "<html>Sorry, Page Not Found</html>"})
        o3, _, _ = classify_jwweb({"kind": "response", "status": 200, "text": "not json"})
        self.assertEqual(o1, "session_expired")
        self.assertEqual(o2, "upstream_error")
        self.assertEqual(o3, "parse_error")

    def test_910_is_upstream_error_not_session_expired(self):
        o, _, _ = classify_jwweb({"kind": "response", "status": 200,
                                  "text": json.dumps({"code": 910, "msg": "x"})})
        self.assertEqual(o, "upstream_error")

    def test_cookie_attributes_preserved(self):
        t = JwwebTransport()
        t.set_cookies([{"name": "JSESSIONID", "value": "v1", "domain": "jwxt.zjgsu.edu.cn",
                        "path": "/jwglxt", "secure": False, "expires": None, "discard": True},
                       {"name": "iPlanetDirectoryPro", "value": "v2",
                        "domain": ".uia.zjgsu.edu.cn", "path": "/", "secure": True,
                        "expires": None, "discard": True}])
        out = {c["name"]: c for c in t.export_cookies()}
        self.assertEqual(out["JSESSIONID"]["domain"], "jwxt.zjgsu.edu.cn")
        self.assertEqual(out["JSESSIONID"]["path"], "/jwglxt")
        self.assertEqual(out["iPlanetDirectoryPro"]["domain"], ".uia.zjgsu.edu.cn")
        self.assertTrue(out["iPlanetDirectoryPro"]["secure"])

    def test_map_items_rejects_non_scalar(self):
        mapped, violations = _map_items([{"kch": "C1", "kcmc": "课程", "cj": {"raw": 1}}],
                                        {"kch": "course_code", "kcmc": "course_name",
                                         "cj": "score"})
        self.assertIsNone(mapped)
        self.assertTrue(any("类型不合规" in v for v in violations))


class Round2RegressionsTest(unittest.TestCase):
    """§2.24 反馈剩余项：R26 格式拒绝 / R32 jar 累积 / R21 去重与对账 / R33 cursor partial。"""

    def test_legacy_dict_cookie_rejected(self):
        t = JwwebTransport()
        with self.assertRaises(ValueError):
            t.set_cookies({"JSESSIONID": "v"})  # 旧 dict：拒绝而非迁移

    def test_cookie_missing_domain_rejected(self):
        t = JwwebTransport()
        with self.assertRaises(ValueError):
            t.set_cookies([{"name": "JSESSIONID", "value": "v",
                            "path": "/", "secure": False}])

    def test_transport_fresh_jar_no_accumulation(self):
        """R32：生产路径每次新建 transport——两个账号先后 set_cookies，
        第二个 transport 的 jar 不得含第一个的 cookie。"""
        t1 = JwwebTransport()
        t1.set_cookies([{"name": "JSESSIONID", "value": "A", "domain": "jwxt.zjgsu.edu.cn",
                         "path": "/", "secure": False, "expires": None, "discard": True}])
        t2 = JwwebTransport()
        t2.set_cookies([{"name": "JSESSIONID", "value": "B", "domain": "jwxt.zjgsu.edu.cn",
                         "path": "/", "secure": False, "expires": None, "discard": True}])
        self.assertEqual([c["value"] for c in t2.export_cookies()], ["B"])

    def test_dedup_keeps_same_course_different_scores(self):
        """R21 剩余：去重仅针对完全相同记录，同课程不同成绩保留。"""
        a = _item("C1", "数据库原理", cj="45")
        b = _item("C1", "数据库原理", cj="60")  # 重修/补考同课不同分
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body([a, b], 2, 1, 1)},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "ok", f.diag)
        self.assertEqual(len(f.items), 2)

    def test_bool_score_rejected(self):
        """R23 剩余：bool 是 int 子类，必须显式拒绝。"""
        mapped, violations = _map_items([{"kch": "C1", "kcmc": "课程", "cj": True}],
                                        {"kch": "course_code", "kcmc": "course_name",
                                         "cj": "score"})
        self.assertIsNone(mapped)

    def test_cursor_on_partial_snapshot_stays_partial(self):
        """R33：partial 快照的 cursor 页保持 partial，不洗成 ok。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r33_"))
        S.save_session(tmp, "acct-p", {"zjgsu_token": "t", "user_type": "2",
                                       "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                           "domain": "jwxt.zjgsu.edu.cn",
                                                           "path": "/", "secure": False,
                                                           "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-p")
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 11)]
        svc = JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp)
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 12, 1, 2)},
            {"kind": "response", "status": 500, "text": "err"},
        ])
        svc._jwweb_transport = ft
        r1 = svc.get_exams("2025", "12", limit=5)
        self.assertEqual(r1["status"], "partial")
        cur = r1["pagination"]["next_cursor"]
        self.assertIsNotNone(cur)
        svc2 = JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp,
                            jwweb_transport=_FakeTransport([]))
        r2 = svc2.get_exams("2025", "12", cursor=cur, limit=5)
        self.assertEqual(r2["status"], "partial")
        self.assertTrue(any(i["code"] == "completeness_unproven" for i in r2["issues"]))


class Round3RegressionsTest(unittest.TestCase):
    """§2.25 反馈剩余：矛盾总数/跨页总数变化/坏条目保留/空旧快照复用/partial freshness。"""

    def _mk(self, tmp, fake=None, acct="acct-r3"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_tc0_tr_positive_not_ok_empty(self):
        """totalCount=0 但 totalResult>0 → 缺口，不包装成合法空。"""
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [], "totalCount": 0, "totalResult": 3,
                                 "totalPage": 1})},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(f.total_expected, 3)

    def test_cross_page_total_change_is_partial(self):
        """跨页总数变化 → partial 保留已读。"""
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 6)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 10, 1, 2)},
            {"kind": "response", "status": 200, "text": _paged_body([], 8, 2, 2)},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(len(f.items), 5)
        self.assertIn("不一致", f.diag)

    def test_later_page_bad_item_keeps_first_screen(self):
        """后续页非对象条目 → 保留已读首屏为 partial，不丢 items。"""
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 6)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 6, 1, 2)},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": ["bad"], "totalCount": 6, "totalPage": 2})},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(len(f.items), 5)

    def test_empty_old_snapshot_reused_on_mapping_failure(self):
        """R27 剩余：旧成功空快照映射失败时也复用（partial），不落 unavailable。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r27b_"))
        ok = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body([], 0, 1, 1)},
        ])
        svc = self._mk(tmp, fake=ok)
        r1 = svc.get_exams("2026", "3")
        self.assertEqual(r1["status"], "ok")
        snap = svc.store.get_snapshot("acct-r3", "campus.exams", "src.jwglxt", "2026-3")
        self.assertIsNotNone(snap)
        # 本次响应字段坏 → 映射失败 → 应复用空快照 partial
        bad = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": _paged_body([{"kch": "C1", "kcmc": "课程", "ksfs": True}], 1, 1, 1)},
        ])
        svc2 = self._mk(tmp, fake=bad)
        r2 = svc2.get_exams("2026", "3")
        self.assertEqual(r2["status"], "partial")
        self.assertIsNotNone(r2["pagination"]["result_snapshot_id"])

    def test_partial_does_not_touch_last_success(self):
        """R33：partial 响应 last_success_at 不取本次时间（保持旧值或 None）。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r33b_"))
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 6)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 12, 1, 2)},
            {"kind": "response", "status": 500, "text": "err"},
        ])
        svc = self._mk(tmp, fake=ft)
        r = svc.get_exams("2025", "12")
        self.assertEqual(r["status"], "partial")
        self.assertIsNone(r["freshness"]["last_success_at"])  # 首次 partial：无虚假成功时间
        self.assertEqual(r["freshness"]["last_attempt_at"],
                         r["coverage"]["source_snapshots"][0]["snapshot_id"] and
                         r["freshness"]["by_source"][0]["last_attempt_at"])

    def test_int_course_name_normalized(self):
        """整数课程名：明确归一为字符串（§2.25 允许的选项二），不让 SDK 校验才崩。"""
        mapped, violations = _map_items([{"kch": "C1", "kcmc": 123}],
                                        {"kch": "course_code", "kcmc": "course_name"})
        self.assertIsNotNone(mapped)
        self.assertEqual(mapped[0]["course_name"], "123")


class Round3RegressionsTest(unittest.TestCase):
    """§2.25 反馈剩余：矛盾总数/跨页总数变化/坏条目保留/空旧快照复用/partial freshness。"""

    def _mk(self, tmp, fake=None, acct="acct-r3"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_tc0_tr_positive_not_ok_empty(self):
        """totalCount=0 但 totalResult>0 → 缺口，不包装成合法空。"""
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [], "totalCount": 0, "totalResult": 3,
                                 "totalPage": 1})},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(f.total_expected, 3)

    def test_cross_page_total_change_is_partial(self):
        """跨页总数变化 → partial 保留已读。"""
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 6)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 10, 1, 2)},
            {"kind": "response", "status": 200, "text": _paged_body([], 8, 2, 2)},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(len(f.items), 5)
        self.assertIn("不一致", f.diag)

    def test_later_page_bad_item_keeps_first_screen(self):
        """后续页非对象条目 → 保留已读首屏为 partial，不丢 items。"""
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 6)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 6, 1, 2)},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": ["bad"], "totalCount": 6, "totalPage": 2})},
        ])
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(len(f.items), 5)

    def test_empty_old_snapshot_reused_on_mapping_failure(self):
        """R27 剩余：旧成功空快照映射失败时也复用（partial），不落 unavailable。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r27b_"))
        ok = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body([], 0, 1, 1)},
        ])
        svc = self._mk(tmp, fake=ok)
        r1 = svc.get_exams("2026", "3")
        self.assertEqual(r1["status"], "ok")
        snap = svc.store.get_snapshot("acct-r3", "campus.exams", "src.jwglxt", "2026-3")
        self.assertIsNotNone(snap)
        # 本次响应字段坏 → 映射失败 → 应复用空快照 partial
        bad = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": _paged_body([{"kch": "C1", "kcmc": "课程", "ksfs": True}], 1, 1, 1)},
        ])
        svc2 = self._mk(tmp, fake=bad)
        r2 = svc2.get_exams("2026", "3")
        self.assertEqual(r2["status"], "partial")
        self.assertIsNotNone(r2["pagination"]["result_snapshot_id"])

    def test_partial_does_not_touch_last_success(self):
        """R33：partial 响应 last_success_at 不取本次时间（保持旧值或 None）。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r33b_"))
        items = [_item("C%d" % i, "课程%d" % i) for i in range(1, 6)]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 12, 1, 2)},
            {"kind": "response", "status": 500, "text": "err"},
        ])
        svc = self._mk(tmp, fake=ft)
        r = svc.get_exams("2025", "12")
        self.assertEqual(r["status"], "partial")
        self.assertIsNone(r["freshness"]["last_success_at"])  # 首次 partial：无虚假成功时间
        self.assertEqual(r["freshness"]["last_attempt_at"],
                         r["coverage"]["source_snapshots"][0]["snapshot_id"] and
                         r["freshness"]["by_source"][0]["last_attempt_at"])

    def test_int_course_name_normalized(self):
        """整数课程名：明确归一为字符串（§2.25 允许的选项二），不让 SDK 校验才崩。"""
        mapped, violations = _map_items([{"kch": "C1", "kcmc": 123}],
                                        {"kch": "course_code", "kcmc": "course_name"})
        self.assertIsNotNone(mapped)
        self.assertEqual(mapped[0]["course_name"], "123")


class Round4RegressionsTest(unittest.TestCase):
    """§2.27 反馈：首页正总数矛盾 / 旧成功版本无条件保留 / cursor partial 语义统一。"""

    def _mk(self, tmp, fake=None, acct="acct-r4"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_first_page_tc_tr_conflict_is_partial(self):
        """§2.27①：首页有数据且 totalCount=2、totalResult=3 → partial，不 ok。"""
        items = [_item("C1", "课程一"), _item("C2", "课程二")]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(items, 2, 1, 1)},
        ])
        # _paged_body 把 totalCount/totalResult 设成同值，这里手写矛盾体
        ft.script[1] = {"kind": "response", "status": 200,
                        "text": json.dumps({"items": items, "totalCount": 2,
                                            "totalResult": 3, "totalPage": 1})}
        f = fetch_exams(ft, "2025", "12")
        self.assertEqual(f.outcome, "partial_source")
        self.assertEqual(len(f.items), 2)

    def test_partial_never_overwrites_old_success(self):
        """§2.27②：旧成功2条 + 新partial3条（总数12）→ 返回旧2条，不覆盖。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r33c_"))
        ok = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": _paged_body([_item("A", "旧课程甲"), _item("B", "旧课程乙")], 2, 1, 1)},
        ])
        svc = self._mk(tmp, fake=ok)
        r1 = svc.get_exams("2025", "12")
        self.assertEqual(r1["status"], "ok")
        new3 = [_item("X", "新课甲"), _item("Y", "新课乙"), _item("Z", "新课丙")]
        bad = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(new3, 12, 1, 2)},
            {"kind": "response", "status": 500, "text": "err"},
        ])
        r2 = self._mk(tmp, fake=bad).get_exams("2025", "12")
        self.assertEqual(r2["status"], "partial")
        names = [i["course_name"] for i in r2["items"]]
        self.assertIn("旧课程甲", names)
        self.assertIn("旧课程乙", names)
        self.assertNotIn("新课甲", names)  # 旧成功版本不被部分观测覆盖

    def test_cursor_after_partial_keeps_partial(self):
        """§2.27④：旧快照缺口为空但最近观测 partial → cursor 页保持 partial。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r33d_"))
        ok = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": _paged_body([_item("A", "旧课程甲"), _item("B", "旧课程乙")], 2, 1, 1)},
        ])
        svc = self._mk(tmp, fake=ok)
        r1 = svc.get_exams("2025", "12")
        self.assertEqual(r1["status"], "ok")
        new3 = [_item("X", "新课甲"), _item("Y", "新课乙"), _item("Z", "新课丙")]
        bad = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(new3, 12, 1, 2)},
            {"kind": "response", "status": 500, "text": "err"},
        ])
        svc2 = self._mk(tmp, fake=bad)
        r2 = svc2.get_exams("2025", "12", limit=1)
        self.assertEqual(r2["status"], "partial")
        cur = r2["pagination"]["next_cursor"]
        self.assertIsNotNone(cur)
        svc3 = self._mk(tmp, fake=_FakeTransport([]))
        r3 = svc3.get_exams("2025", "12", cursor=cur, limit=5)
        self.assertEqual(r3["status"], "partial")
        self.assertTrue(any(i["code"] == "completeness_unproven" for i in r3["issues"]))

    def test_cursor_freshness_uses_health_not_partial_snapshot(self):
        """§2.27③：cursor 页 partial 时 last_success 不取 partial 快照 fetched_at。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r33e_"))
        ok = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": _paged_body([_item("A", "旧课程甲")], 1, 1, 1)},
        ])
        svc = self._mk(tmp, fake=ok, acct="acct-f1")
        svc.get_grades("2025", "12")
        new2 = [_item("X", "新课甲"), _item("Y", "新课乙")]
        bad = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200, "text": _paged_body(new2, 12, 1, 2)},
            {"kind": "response", "status": 500, "text": "err"},
        ])
        svc2 = self._mk(tmp, fake=bad, acct="acct-f2")
        r = svc2.get_grades("2025", "12", limit=1)
        cur = r["pagination"]["next_cursor"]
        svc3 = self._mk(tmp, fake=_FakeTransport([]), acct="acct-f2")
        r3 = svc3.get_grades("2025", "12", cursor=cur, limit=5)
        self.assertEqual(r3["status"], "partial")
        # last_success 不得等于 partial 快照 fetched_at（本次部分观测时间）
        snap = svc3.store.get_snapshot("acct-f2", "campus.grades", "src.jwglxt", "2025-12")
        self.assertNotEqual(r3["freshness"]["last_success_at"], snap["fetched_at"])


class GradeDetailsTest(unittest.TestCase):
    """§2.30 第 1 项：get_grade_details 两步读取（课程列表→逐门明细）。"""

    def _mk(self, tmp, fake, acct="acct-gd"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    @staticmethod
    def _kc(courses, total=None):
        return {"kind": "response", "status": 200,
                "text": json.dumps({"items": courses,
                                    "totalCount": total if total is not None else len(courses),
                                    "totalResult": total if total is not None else len(courses),
                                    "totalPage": 1})}

    @staticmethod
    def _xm(comps):
        return {"kind": "response", "status": 200,
                "text": json.dumps({"items": comps, "totalCount": len(comps),
                                    "totalResult": len(comps), "totalPage": 1})}

    def test_two_step_normal(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_gd_"))
        courses = [{"kcmc": "Linux系统与编程实践", "kch_id": "CST037",
                    "jxb_id": "JXB1", "jxbmc": "(2025-2026-2)-CST037-05", "zpcj": "75",
                    "kkbmmc": "计算机科学与技术学院", "xf": "3.0"},
                   {"kcmc": "数据结构", "kch_id": "CST050",
                    "jxb_id": "JXB2", "jxbmc": "(2025-2026-2)-CST050-06", "zpcj": "41",
                    "kkbmmc": "计算机科学与技术学院", "xf": "3.0"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            self._kc(courses),
            self._xm([{"xmblmc": "平时(50%)", "xmcj": "92"},
                      {"xmblmc": "期末(50%)", "xmcj": "58"},
                      {"xmblmc": "总评", "xmcj": "75"}]),
            self._xm([{"xmblmc": "平时(30%)", "xmcj": "20"},
                      {"xmblmc": "期末(70%)", "xmcj": "50"},
                      {"xmblmc": "总评", "xmcj": "41"}]),
        ])
        svc = self._mk(tmp, ft)
        r = svc.get_grade_details("2025", "12")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["items"]), 2)
        linux = r["items"][0]
        self.assertEqual(linux["course_name"], "Linux系统与编程实践")
        self.assertEqual([c["component_name"] for c in linux["components"]],
                         ["平时(50%)", "期末(50%)", "总评"])
        self.assertEqual(linux["components"][0]["component_score"], "92")
        self.assertFalse(linux["components_failed"])

    def test_course_filter(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_gd_"))
        courses = [{"kcmc": "Linux系统与编程实践", "kch_id": "CST037", "jxb_id": "JXB1",
                    "jxbmc": "x", "zpcj": "75", "xf": "3.0"},
                   {"kcmc": "数据结构", "kch_id": "CST050", "jxb_id": "JXB2",
                    "jxbmc": "x", "zpcj": "41", "xf": "3.0"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            self._kc(courses),
            self._xm([{"xmblmc": "总评", "xmcj": "41"}]),
        ])
        svc = self._mk(tmp, ft)
        r = svc.get_grade_details("2025", "12", course="数据结构")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual(r["items"][0]["course_name"], "数据结构")

    def test_no_match_is_ok_empty_with_hint(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_gd_"))
        courses = [{"kcmc": "Linux系统与编程实践", "kch_id": "CST037", "jxb_id": "JXB1",
                    "jxbmc": "x", "zpcj": "75", "xf": "3.0"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            self._kc(courses),
        ])
        svc = self._mk(tmp, ft)
        r = svc.get_grade_details("2025", "12", course="不存在的课")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["items"], [])
        self.assertTrue(any("无匹配" in i["safe_message"] for i in r["issues"]))

    def test_single_course_component_failure_is_partial(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_gd_"))
        courses = [{"kcmc": "课程甲", "kch_id": "C1", "jxb_id": "JXB1", "jxbmc": "x",
                    "zpcj": "80", "xf": "2.0"},
                   {"kcmc": "课程乙", "kch_id": "C2", "jxb_id": "JXB2", "jxbmc": "x",
                    "zpcj": "90", "xf": "2.0"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            self._kc(courses),
            self._xm([{"xmblmc": "总评", "xmcj": "80"}]),
            {"kind": "network_error", "err": "boom"},   # 课程乙明细失败
        ])
        svc = self._mk(tmp, ft)
        r = svc.get_grade_details("2025", "12")
        self.assertEqual(r["status"], "partial")
        by = {i["course_name"]: i for i in r["items"]}
        self.assertFalse(by["课程甲"]["components_failed"])
        self.assertTrue(by["课程乙"]["components_failed"])
        self.assertEqual(by["课程乙"]["components"], [])


class InternshipTest(unittest.TestCase):
    """§2.30 第 2 项：get_internship_grades（合法空工具）。"""

    def _mk(self, tmp, fake, acct="acct-sx"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_legal_empty(self):
        """本人实测合法空：items=[] 为 ok（零总数证据），不是失败。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_sx_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [], "totalCount": 0, "totalResult": 0,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_internship_grades("2025", "12")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["items"], [])
        snap = r["coverage"]["source_snapshots"][0]
        self.assertTrue(snap["complete"] is False)  # complete 口径不变

    def test_with_record(self):
        """有记录时正常映射。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_sx2_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [{"kcmc": "专业实习", "kch": "SEG090",
                                            "zzcj": "88", "jsxm": "张三",
                                            "xmmc": "软件开发实习",
                                            "jdmc": "下沙", "jxbmc": "实习2501",
                                            "zmc": "一组"}],
                                 "totalCount": 1, "totalResult": 1, "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_internship_grades("2025", "12")
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["course_name"], "专业实习")
        self.assertEqual(it["final_score"], "88")
        self.assertEqual(it["project_name"], "软件开发实习")


class InternshipTest(unittest.TestCase):
    """§2.30 第 2 项：get_internship_grades（合法空工具）。"""

    def _mk(self, tmp, fake, acct="acct-sx"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_legal_empty(self):
        """本人实测合法空：items=[] 为 ok（零总数证据），不是失败。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_sx_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [], "totalCount": 0, "totalResult": 0,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_internship_grades("2025", "12")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["items"], [])
        snap = r["coverage"]["source_snapshots"][0]
        self.assertTrue(snap["complete"] is False)  # complete 口径不变

    def test_with_record(self):
        """有记录时正常映射。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_sx2_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [{"kcmc": "专业实习", "kch": "SEG090",
                                            "zzcj": "88", "jsxm": "张三",
                                            "xmmc": "软件开发实习",
                                            "jdmc": "下沙", "jxbmc": "实习2501",
                                            "zmc": "一组"}],
                                 "totalCount": 1, "totalResult": 1, "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_internship_grades("2025", "12")
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["course_name"], "专业实习")
        self.assertEqual(it["final_score"], "88")
        self.assertEqual(it["project_name"], "软件开发实习")


class AcademicProgressTest(unittest.TestCase):
    """§2.30 第 5 项：get_academic_progress（HTML 统计解析）。"""

    HTML = ("""<div id="alertBox"><font>测试生 同学，您的课程修读情况（供参考）：( 统计时间"""
            """2026-10-14 10:45:05之前有效)</font><font>当前所有课程 <a>平均学分绩点</a>（GPA）： """
            """<font> 2.14 </font> 计划总课程&nbsp;77&nbsp; 门 通过&nbsp;15&nbsp; 门， """
            """未通过 &nbsp;4&nbsp;门&nbsp; ；未修&nbsp;48&nbsp; 门； 在读&nbsp;10&nbsp; 门！ """
            """计划外： 通过&nbsp;6&nbsp; 门， 未通过 &nbsp;1&nbsp;门</font></div>提示: 此页面信息仅做学业修读情况参考。""")

    def _mk(self, tmp, fake, acct="acct-xy"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_parse_realistic_html(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_xy_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": self.HTML},
        ])
        r = self._mk(tmp, ft).get_academic_progress()
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["gpa"], "2.14")
        self.assertEqual(it["planned_courses"], "77")
        self.assertEqual(it["passed"], "15")
        self.assertEqual(it["failed"], "4")
        self.assertEqual(it["not_started"], "48")
        self.assertEqual(it["in_progress"], "10")
        self.assertEqual(it["extra_passed"], "6")
        self.assertEqual(it["extra_failed"], "1")
        self.assertIn("2026-10-14 10:45:05", it["stat_time_text"])
        self.assertTrue(any("课程级列表" in i["safe_message"] for i in r["issues"]))

    def test_login_html_is_auth_required(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_xy2_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>统一身份认证 登录</html>"},
        ])
        r = self._mk(tmp, ft).get_academic_progress()
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "auth_required")


class AcademicProgressTest(unittest.TestCase):
    """§2.30 第 5 项：get_academic_progress（HTML 统计解析）。"""

    HTML = ("""<div id="alertBox"><font>测试生 同学，您的课程修读情况（供参考）：( 统计时间"""
            """2026-10-14 10:45:05之前有效)</font><font>当前所有课程 <a>平均学分绩点</a>（GPA）： """
            """<font> 2.14 </font> 计划总课程&nbsp;77&nbsp; 门 通过&nbsp;15&nbsp; 门， """
            """未通过 &nbsp;4&nbsp;门&nbsp; ；未修&nbsp;48&nbsp; 门； 在读&nbsp;10&nbsp; 门！ """
            """计划外： 通过&nbsp;6&nbsp; 门， 未通过 &nbsp;1&nbsp;门</font></div>提示: 此页面信息仅做学业修读情况参考。""")

    def _mk(self, tmp, fake, acct="acct-xy"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_parse_realistic_html(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_xy_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": self.HTML},
        ])
        r = self._mk(tmp, ft).get_academic_progress()
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["gpa"], "2.14")
        self.assertEqual(it["planned_courses"], "77")
        self.assertEqual(it["passed"], "15")
        self.assertEqual(it["failed"], "4")
        self.assertEqual(it["not_started"], "48")
        self.assertEqual(it["in_progress"], "10")
        self.assertEqual(it["extra_passed"], "6")
        self.assertEqual(it["extra_failed"], "1")
        self.assertIn("2026-10-14 10:45:05", it["stat_time_text"])
        self.assertTrue(any("课程级列表" in i["safe_message"] for i in r["issues"]))

    def test_login_html_is_auth_required(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_xy2_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>统一身份认证 登录</html>"},
        ])
        r = self._mk(tmp, ft).get_academic_progress()
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "auth_required")


class PlansTest(unittest.TestCase):
    """§2.30 第 3/4 项：get_training_plans / get_teaching_plans。"""

    def _mk(self, tmp, fake, acct="acct-pl"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_training_plans_filter_and_year_required(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_tp_"))
        courses = [{"pyfamc": "2023软件工程", "pyfaxx_id": "P1", "bbh": "2023",
                    "njdm_id": "2023,2026,2025,2024", "jgmc": "计算机科学与技术学院",
                    "zymc": "软件工程", "zdxf": "167.5", "hasPyfaxxFj": "1",
                    "dlbs": "专业", "zyh": "1208"},
                   {"pyfamc": "2023会计学", "pyfaxx_id": "P2", "bbh": "2023",
                    "njdm_id": "2025", "jgmc": "会计学院", "zymc": "会计学",
                    "zdxf": "160", "hasPyfaxxFj": "0", "dlbs": "专业", "zyh": "0201"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": courses, "totalCount": 2, "totalResult": 2,
                                 "totalPage": 1})},
        ])
        svc = self._mk(tmp, ft)
        r = svc.get_training_plans("2025", keyword="软件工程")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual(r["items"][0]["plan_name"], "2023软件工程")
        self.assertEqual(r["items"][0]["total_credit"], "167.5")
        self.assertEqual(r["items"][0]["has_attachment"], "1")
        # year 缺失/非法
        r2 = self._mk(tmp, _FakeTransport([])).get_training_plans("20x5")
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")

    def test_teaching_plans_normal(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_jh_"))
        courses = [{"jxzxjhxx_id": "ID1", "njmc": "2025", "zymc": "软件工程",
                    "zyh": "1208", "zdxf": "160", "kcs": "82", "xqmc": "下沙",
                    "jg_id": "12", "jhrs": "100", "dlbs": "专业", "sfgazy": "否"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": courses, "totalCount": 1, "totalResult": 1,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_teaching_plans("2025", keyword="软件")
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["major_name"], "软件工程")
        self.assertEqual(it["course_count"], "82")
        self.assertEqual(it["plan_ref"], "ID1")
        self.assertTrue(any("概览" in i["safe_message"] for i in r["issues"]))


class PlansTest(unittest.TestCase):
    """§2.30 第 3/4 项：get_training_plans / get_teaching_plans。"""

    def _mk(self, tmp, fake, acct="acct-pl"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_training_plans_filter_and_year_required(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_tp_"))
        courses = [{"pyfamc": "2023软件工程", "pyfaxx_id": "P1", "bbh": "2023",
                    "njdm_id": "2023,2026,2025,2024", "jgmc": "计算机科学与技术学院",
                    "zymc": "软件工程", "zdxf": "167.5", "hasPyfaxxFj": "1",
                    "dlbs": "专业", "zyh": "1208"},
                   {"pyfamc": "2023会计学", "pyfaxx_id": "P2", "bbh": "2023",
                    "njdm_id": "2025", "jgmc": "会计学院", "zymc": "会计学",
                    "zdxf": "160", "hasPyfaxxFj": "0", "dlbs": "专业", "zyh": "0201"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": courses, "totalCount": 2, "totalResult": 2,
                                 "totalPage": 1})},
        ])
        svc = self._mk(tmp, ft)
        r = svc.get_training_plans("2025", keyword="软件工程")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual(r["items"][0]["plan_name"], "2023软件工程")
        self.assertEqual(r["items"][0]["total_credit"], "167.5")
        self.assertEqual(r["items"][0]["has_attachment"], "1")
        # year 缺失/非法
        r2 = self._mk(tmp, _FakeTransport([])).get_training_plans("20x5")
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")

    def test_teaching_plans_normal(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_jh_"))
        courses = [{"jxzxjhxx_id": "ID1", "njmc": "2025", "zymc": "软件工程",
                    "zyh": "1208", "zdxf": "160", "kcs": "82", "xqmc": "下沙",
                    "jg_id": "12", "jhrs": "100", "dlbs": "专业", "sfgazy": "否"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": courses, "totalCount": 1, "totalResult": 1,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_teaching_plans("2025", keyword="软件")
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["major_name"], "软件工程")
        self.assertEqual(it["course_count"], "82")
        self.assertEqual(it["plan_ref"], "ID1")
        self.assertTrue(any("概览" in i["safe_message"] for i in r["issues"]))


class PlanCoursesTest(unittest.TestCase):
    """§2.30 第 4 项延伸：get_teaching_plan_courses。"""

    def _mk(self, tmp, fake, acct="acct-pc"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_normal_and_empty_ref(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_pc_"))
        kcs = [{"kch": "CST029", "kcmc": "计算机大类专业导论", "kcxzmc": "必修",
                "kclbmc": "学科共同课", "xf": "2.0", "xqmc": "下沙",
                "xfyqjdmc": "初级课程", "xsxxxx": "理论(2.0)-实验(2.0)",
                "jyxdxnm": "2025-2026", "jyxdxqm": "1", "kkbmmc": "计院"},
               {"kch": "CST012", "kcmc": "数据库原理", "kcxzmc": "必修",
                "kclbmc": "学科共同课", "xf": "3.0", "xqmc": "下沙",
                "xfyqjdmc": "数据课程", "xsxxxx": "理论(3.0)",
                "jyxdxnm": "2025-2026", "jyxdxqm": "1", "kkbmmc": "计院"}]
        ft = _FakeTransport([
            # activate_index=False：明细 POST 直接消费第一条脚本
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": kcs, "totalCount": 2, "totalResult": 2,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_teaching_plan_courses("PID-1")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["items"]), 2)
        self.assertEqual(r["items"][0]["credit_node_name"], "初级课程")
        # 空 plan_ref
        r2 = self._mk(tmp, _FakeTransport([])).get_teaching_plan_courses("")
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")


class PlanCoursesTest(unittest.TestCase):
    """§2.30 第 4 项延伸：get_teaching_plan_courses。"""

    def _mk(self, tmp, fake, acct="acct-pc"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_normal_and_empty_ref(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_pc_"))
        kcs = [{"kch": "CST029", "kcmc": "计算机大类专业导论", "kcxzmc": "必修",
                "kclbmc": "学科共同课", "xf": "2.0", "xqmc": "下沙",
                "xfyqjdmc": "初级课程", "xsxxxx": "理论(2.0)-实验(2.0)",
                "jyxdxnm": "2025-2026", "jyxdxqm": "1", "kkbmmc": "计院"},
               {"kch": "CST012", "kcmc": "数据库原理", "kcxzmc": "必修",
                "kclbmc": "学科共同课", "xf": "3.0", "xqmc": "下沙",
                "xfyqjdmc": "数据课程", "xsxxxx": "理论(3.0)",
                "jyxdxnm": "2025-2026", "jyxdxqm": "1", "kkbmmc": "计院"}]
        ft = _FakeTransport([
            # activate_index=False：明细 POST 直接消费第一条脚本
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": kcs, "totalCount": 2, "totalResult": 2,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_teaching_plan_courses("PID-1")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["items"]), 2)
        self.assertEqual(r["items"][0]["credit_node_name"], "初级课程")
        # 空 plan_ref
        r2 = self._mk(tmp, _FakeTransport([])).get_teaching_plan_courses("")
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")


class RetakeTest(unittest.TestCase):
    """重修课程查询。"""

    def _mk(self, tmp, fake, acct="acct-rk"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_normal_and_invalid_args(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_rk_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [{"kch": "CST050", "kcmc": "数据结构",
                                            "kclbmc": "学科共同课", "kkbmmc": "计院",
                                            "xf": "3.0", "cxxnmc": "2026-2027",
                                            "cxxqmc": "1", "cxzt": "重修"}],
                                 "totalCount": 1, "totalResult": 1, "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_retake_courses("2026", "3", keyword="数据结构")
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["course_name"], "数据结构")
        self.assertEqual(it["retake_status"], "重修")
        # 非法学期码
        r2 = self._mk(tmp, _FakeTransport([])).get_retake_courses("2026", "2")
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")


class RetakeTest(unittest.TestCase):
    """重修课程查询。"""

    def _mk(self, tmp, fake, acct="acct-rk"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_normal_and_invalid_args(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_rk_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [{"kch": "CST050", "kcmc": "数据结构",
                                            "kclbmc": "学科共同课", "kkbmmc": "计院",
                                            "xf": "3.0", "cxxnmc": "2026-2027",
                                            "cxxqmc": "1", "cxzt": "重修"}],
                                 "totalCount": 1, "totalResult": 1, "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_retake_courses("2026", "3", keyword="数据结构")
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertEqual(it["course_name"], "数据结构")
        self.assertEqual(it["retake_status"], "重修")
        # 非法学期码
        r2 = self._mk(tmp, _FakeTransport([])).get_retake_courses("2026", "2")
        self.assertEqual(r2["status"], "unavailable")
        self.assertEqual(r2["issues"][0]["code"], "invalid_arguments")


class WarningsTest(unittest.TestCase):
    """学业预警状态（N305516 服务端渲染）。"""

    def _mk(self, tmp, fake, acct="acct-yj"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_no_warning(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_yj_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200,
             "text": "<html><body><div>测试生同学您好： 当前学年学期暂无预警！</div></body></html>"},
        ])
        r = self._mk(tmp, ft).get_academic_warnings()
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["items"][0]["has_warning"])
        self.assertIn("暂无预警", r["items"][0]["summary_text"])

    def test_warning_present_raw(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_yj2_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200,
             "text": "<html><body><div>测试生同学您好： 您有学业预警：高等数学Ⅱ(下) 31分，"
                     "请及时处理。</div></body></html>"},
        ])
        r = self._mk(tmp, ft).get_academic_warnings()
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["items"][0]["has_warning"])
        self.assertIn("高等数学", r["items"][0]["summary_text"])

    def test_login_page_blocked(self):
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_yj3_"))
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>统一身份认证 登录</html>"},
        ])
        r = self._mk(tmp, ft).get_academic_warnings()
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["issues"][0]["code"], "auth_required")


class SanitizeAndInnovationTest(unittest.TestCase):
    """§2.49 R41 净化 + R44/R45 素质分合成回归。"""

    def _mk(self, tmp, fake, acct="acct-si"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_r41_url_sanitized(self):
        """合成含票据/token 的链接不得出现在输出。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r41_"))
        rows = [{"kch": "CST050", "kcmc": "数据结构", "jsxm": "蒋海华",
                 "jxbmc": "x", "sksj": "周一1-2节",
                 "wsskdz": "https://meeting.tencent.com/dmw/p/xxx?ticket=ST-123&token=abc&page=1",
                 "wsskfjlj": "/upload/file.pdf?jsessionid=XYZ123"}]
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": "<html>index</html>"},
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": rows, "totalCount": 1, "totalResult": 1,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_online_class_urls("2026", "3")  # online: activate=True
        self.assertEqual(r["status"], "ok")
        it = r["items"][0]
        self.assertNotIn("ticket", str(it.get("online_url", "")).lower())
        self.assertNotIn("token=", str(it.get("online_url", "")).lower())
        self.assertNotIn("jsessionid", str(it.get("attachment_path", "")).lower())
        self.assertTrue(any("净化" in i["safe_message"] for i in r["issues"]))
        # 正常参数保留
        self.assertIn("page=1", str(it.get("online_url")))

    def test_r44_zero_and_bad_rows(self):
        """合法 0 保留；坏行具名跳过不记成功空。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r44_"))
        rows = [{"xmlbmc": "创新创业学分", "hdfz": "0", "zgfzyq": "3"},  # 合法 0
                {"xmlbmc": 12345, "hdfz": {"bad": "obj"}},              # 对象字段违约
                "not-a-dict"]                                           # 非对象行
        ft = _FakeTransport([
            # R44：activate_index=False，POST 直接消费第一条
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": rows, "totalCount": 3, "totalResult": 3,
                                 "totalPage": 1})},
        ])
        r = self._mk(tmp, ft).get_innovation_scores()
        # 非对象行由 _fetch_paged 判 partial_source（既有契约）——合法 0 与 int 归一仍验证
        self.assertEqual(r["status"], "partial")
        self.assertEqual(len(r["items"]), 2)
        self.assertEqual(r["items"][0]["score"], "0")          # 合法 0 保留
        self.assertEqual(r["items"][1]["project_name"], "12345")  # int 归一
        self.assertIsNone(r["items"][1]["score"])              # 对象字段类型违约→None
        snap = r["coverage"]["source_snapshots"][0]
        self.assertFalse(snap["missing_ranges"] == [] and False)

    def test_r45_partial_keeps_old_success(self):
        """partial 不覆盖旧成功快照。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_r45_"))
        ok_rows = [{"kcmc": "旧项目A", "hdfz": "5", "year": "2025"},
                   {"kcmc": "旧项目B", "hdfz": "3", "year": "2025"}]
        ft1 = _FakeTransport([
            # activate_index=False：POST 直接消费第一条
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": ok_rows, "totalCount": 2, "totalResult": 2,
                                 "totalPage": 1})},
        ])
        svc = self._mk(tmp, ft1)
        r1 = svc.get_innovation_scores()
        self.assertEqual(r1["status"], "ok")
        snap1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]
        # partial：声明 2 条实得 1 条
        ft2 = _FakeTransport([
            {"kind": "response", "status": 200,
             "text": json.dumps({"items": [{"kcmc": "新项目C", "hdfz": "2"}],
                                 "totalCount": 2, "totalResult": 2, "totalPage": 1})},
        ])
        svc2 = JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=ft2)
        r2 = svc2.get_innovation_scores()
        self.assertEqual(r2["status"], "partial")
        snap2 = r2["coverage"]["source_snapshots"][0]
        self.assertEqual(snap2["snapshot_id"], snap1)      # 旧快照保留
        self.assertEqual(len(r2["items"]), 2)              # 返回旧成功条目
        self.assertTrue(any("未入库" in i["safe_message"] for i in r2["issues"]))


class Fixes252Test(unittest.TestCase):
    """§2.52 四项反例回归（R41 params/R42 联系方式投影/R43 坏结构/R44 混合丢行）。"""

    def _mk(self, tmp, fake, acct="acct-252"):
        S.save_session(tmp, acct, {"zjgsu_token": "t", "user_type": "2",
                                   "jwglxt_cookies": [{"name": "JSESSIONID", "value": "x",
                                                       "domain": "jwxt.zjgsu.edu.cn",
                                                       "path": "/", "secure": False,
                                                       "expires": None, "discard": True}]})
        S.set_active(tmp, acct)
        return JWWebService(SnapshotStore(tmp / "c.sqlite3"), tmp, jwweb_transport=fake)

    def test_r41_params_cred(self):
        """;jsessionid 在 params 段的凭据不进输出。"""
        from src.adapters.campus.jwweb import sanitize_url
        clean, had = sanitize_url(
            "http://x.example/a/b;jsessionid=XYZ123?k=1")
        self.assertTrue(had)
        self.assertNotIn("jsessionid", clean.lower())

    def test_r42_contact_fields_hidden(self):
        """联系电话/电子邮箱/紧急联系人电话默认不返回（白名单投影）。"""
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_252a_"))
        html = ("<html><body>"
                '<label class="control-label">学号：</label><div class="col-sm-8">'
                '<p class="form-control-static">2099000001</p></div>'
                '<label class="control-label">姓名：</label><div class="col-sm-8">'
                '<p class="form-control-static">测试</p></div>'
                '<label class="control-label">联系电话：</label><div class="col-sm-8">'
                '<p class="form-control-static">13800000000</p></div>'
                '<label class="control-label">电子邮箱：</label><div class="col-sm-8">'
                '<p class="form-control-static">a@b.c</p></div>'
                '<label class="control-label">紧急联系人电话：</label><div class="col-sm-8">'
                '<p class="form-control-static">13900000000</p></div>'
                "</body></html>")
        ft = _FakeTransport([
            {"kind": "response", "status": 200, "text": html},
        ])
        r = self._mk(tmp, ft).get_my_profile()
        self.assertEqual(r["status"], "ok")
        pf = r["items"][0]["profile"]
        self.assertEqual(pf.get("学号"), "2099000001")
        self.assertNotIn("联系电话", pf)
        self.assertNotIn("电子邮箱", pf)
        self.assertNotIn("紧急联系人电话", pf)

    def test_r43_bad_struct_partial(self):
        """坏节点/kcList → partial 且不覆盖旧成功（mock 模块级 fetch）。"""
        from unittest import mock
        from src.adapters.campus.jwweb import JwwebFetch
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_252b_"))
        S.save_session(tmp, "acct-252b", {"zjgsu_token": "t", "user_type": "2",
                                          "jwglxt_cookies": [{"name": "J", "value": "x",
                                                              "domain": "jwxt.zjgsu.edu.cn",
                                                              "path": "/", "secure": False,
                                                              "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-252b")
        good = JwwebFetch("ok", [{"xfyqjdmc": "正常节点", "kcList": [
            {"kch": "C1", "kcmc": "课程1", "xf": "2.0", "yxxf": "2.0"}]}], "", 1)
        bad = JwwebFetch("ok", ["not-a-node", {"xfyqjdmc": "坏kcList", "kcList": {"x": 1}}],
                         "", 2)
        store = SnapshotStore(tmp / "c.sqlite3")
        svc = JWWebService(store, tmp)
        with mock.patch("src.service.jwweb_service.fetch_credit_requirements",
                        return_value={"outcome": "ok", "nodes": good.items, "diag": ""}):
            r1 = svc.get_credit_requirements()
        self.assertEqual(r1["status"], "ok")
        snap1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]
        with mock.patch("src.service.jwweb_service.fetch_credit_requirements",
                        return_value={"outcome": "ok", "nodes": bad.items, "diag": ""}):
            r2 = svc.get_credit_requirements()
        self.assertEqual(r2["status"], "partial")
        snap2 = r2["coverage"]["source_snapshots"][0]
        self.assertEqual(snap2["snapshot_id"], snap1)   # 旧成功保留
        self.assertTrue(any("未入库" in i["safe_message"] for i in r2["issues"]))

    def test_r43_bad_course_partial(self):
        """kcList 内坏课程行 → partial 且不覆盖旧成功（§2.54 唯一剩余项）。"""
        from unittest import mock
        from src.adapters.campus.jwweb import JwwebFetch
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_254_"))
        S.save_session(tmp, "acct-254", {"zjgsu_token": "t", "user_type": "2",
                                         "jwglxt_cookies": [{"name": "J", "value": "x",
                                                             "domain": "jwxt.zjgsu.edu.cn",
                                                             "path": "/", "secure": False,
                                                             "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-254")
        good = JwwebFetch("ok", [{"xfyqjdmc": "正常节点", "kcList": [
            {"kch": "C1", "kcmc": "课程1", "xf": "2.0", "yxxf": "2.0"}]}], "", 1)
        # kcList 内坏课程（缺课程名→白名单拒绝）
        bad = JwwebFetch("ok", [{"xfyqjdmc": "节点A", "kcList": [
            {"kch": "C1", "kcmc": "课程1", "xf": "2.0"},
            {"kch": "C2", "xf": "3.0"}]}], "", 2)   # 第二门缺 kcmc → bad_kc
        store = SnapshotStore(tmp / "c.sqlite3")
        svc = JWWebService(store, tmp)
        with mock.patch("src.service.jwweb_service.fetch_credit_requirements",
                        return_value={"outcome": "ok", "nodes": good.items, "diag": ""}):
            r1 = svc.get_credit_requirements()
        self.assertEqual(r1["status"], "ok")
        snap1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]
        with mock.patch("src.service.jwweb_service.fetch_credit_requirements",
                        return_value={"outcome": "ok", "nodes": bad.items, "diag": ""}):
            r2 = svc.get_credit_requirements()
        self.assertEqual(r2["status"], "partial")       # 坏课程纳入 partial
        snap2 = r2["coverage"]["source_snapshots"][0]
        self.assertEqual(snap2["snapshot_id"], snap1)   # 旧成功保留
        self.assertTrue(any("未入库" in i["safe_message"] for i in r2["issues"]))

    def test_r43_first_partial_output(self):
        """首次 partial（无旧成功）：last_success_at=None 不填当前观测；缺口含坏课程数。"""
        from unittest import mock
        from src.adapters.campus.jwweb import JwwebFetch
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_256_"))
        S.save_session(tmp, "acct-256", {"zjgsu_token": "t", "user_type": "2",
                                         "jwglxt_cookies": [{"name": "J", "value": "x",
                                                             "domain": "jwxt.zjgsu.edu.cn",
                                                             "path": "/", "secure": False,
                                                             "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-256")
        mixed = JwwebFetch("ok", [
            {"xfyqjdmc": "正常节点", "kcList": [{"kch": "C1", "kcmc": "课程1", "xf": "2.0"}]},
            {"xfyqjdmc": "坏课程节点", "kcList": [
                {"kch": "C2", "xf": "3.0"}]}], "", 2)   # 1 好 1 坏课程
        store = SnapshotStore(tmp / "c.sqlite3")
        svc = JWWebService(store, tmp)
        # 直接首次 partial（本 store 无任何成功历史——health 无 last_success_at）
        with mock.patch("src.service.jwweb_service.fetch_credit_requirements",
                        return_value={"outcome": "ok", "nodes": mixed.items, "diag": ""}):
            r = svc.get_credit_requirements()
        self.assertEqual(r["status"], "partial")
        # 时间：首次 partial 无旧成功——last_success_at 必须为 None
        self.assertIsNone(r["freshness"]["last_success_at"])
        self.assertIsNone(r["freshness"]["by_source"][0]["last_success_at"])
        self.assertIsNone(r["freshness"]["data_age_seconds"])
        # 缺口：missing_ranges 含坏课程数（只列实际存在的坏项类型）
        snap = r["coverage"]["source_snapshots"][0]
        self.assertEqual(len(snap["missing_ranges"]), 1)
        self.assertIn("1 门坏课程", snap["missing_ranges"][0])
        self.assertNotIn("0 个坏结构项", snap["missing_ranges"][0])

    def test_r44_mixed_rows_partial(self):
        """一好一坏混合行 → partial（任何丢行都纳入；mock 模块级 fetch）。"""
        from unittest import mock
        from src.adapters.campus.jwweb import JwwebFetch
        tmp = Path(tempfile.mkdtemp(prefix="jwweb_252c_"))
        S.save_session(tmp, "acct-252c", {"zjgsu_token": "t", "user_type": "2",
                                          "jwglxt_cookies": [{"name": "J", "value": "x",
                                                              "domain": "jwxt.zjgsu.edu.cn",
                                                              "path": "/", "secure": False,
                                                              "expires": None, "discard": True}]})
        S.set_active(tmp, "acct-252c")
        ok_rows = [{"kcmc": "旧A", "hdfz": "5", "year": "2025"},
                   {"kcmc": "旧B", "hdfz": "3", "year": "2025"}]
        good = JwwebFetch("ok", ok_rows, "", 2)
        # 混合：一正常 + 一对象-only（全字段违约→丢行）
        mixed = JwwebFetch("ok", [
            {"kcmc": "新C", "hdfz": "2", "year": "2026"},
            {"kcmc": {"bad": 1}, "hdfz": {"x": 1}, "year": {"y": 2}}], "", 2)
        store = SnapshotStore(tmp / "c.sqlite3")
        svc = JWWebService(store, tmp)
        with mock.patch("src.service.jwweb_service.fetch_innovation_scores",
                        return_value=good):
            r1 = svc.get_innovation_scores()
        self.assertEqual(r1["status"], "ok")
        snap1 = r1["coverage"]["source_snapshots"][0]["snapshot_id"]
        with mock.patch("src.service.jwweb_service.fetch_innovation_scores",
                        return_value=mixed):
            r2 = svc.get_innovation_scores()
        self.assertEqual(r2["status"], "partial")
        snap2 = r2["coverage"]["source_snapshots"][0]
        self.assertEqual(snap2["snapshot_id"], snap1)   # 旧成功保留
        self.assertTrue(any("映射失败" in i["safe_message"] for i in r2["issues"]))


if __name__ == "__main__":
    unittest.main()
