# -*- coding: utf-8 -*-
"""P0 激活记忆（默认关闭）合成反例。

覆盖设计稿 docs/激活记忆设计-2026-10-18.md 的反例清单：
- 同功能码第二次跳过 GET；跨功能码各自激活一次；
- Cookie 轮换重新激活；
- 910 只重试一次；
- 跳过激活后空结果先重激活核验；
- 登录页/5xx 不标记；
- 开关关闭时始终 GET；
- 并发同 key 只 GET 一次；
- 账本 TTL/容量。
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from src.adapters.campus import activation
from src.adapters.campus.jwweb import (EXAM_GNM, EXAM_INDEX_PATH, EXAM_PATH,
                                       GRADE_GNM, GRADE_INDEX_PATH, GRADE_PATH,
                                       TIMETABLE_GNM, TIMETABLE_INDEX_PATH,
                                       TIMETABLE_PATH,
                                       _fetch_paged, raw_business_code)
from src.domain.models import MODULE_GRADES, SOURCE_JWGLXT


def _resp(obj):
    return {"kind": "response", "status": 200,
            "text": json.dumps(obj, ensure_ascii=False)}


def _ok(n=1):
    items = [{"kcmc": "课程%d" % i} for i in range(n)]
    return _resp({"items": items, "totalCount": n, "totalResult": n,
                  "totalPage": 1})


def _empty():
    return _resp({"items": [], "totalCount": 0, "totalResult": 0,
                  "totalPage": 1})


def _code_910():
    return _resp({"code": 910, "items": [], "totalCount": 0,
                  "totalResult": 0, "totalPage": 1})


def _login_html():
    return {"kind": "response", "status": 200,
            "text": "<html>统一身份认证 cas/login 请登录</html>"}


class _FakeTransport:
    def __init__(self, script=None, fp="fp-1", get_script=None):
        self.script = list(script or [])
        self.get_script = list(get_script or [])
        self.fp = fp
        self.calls = []
        self.lock = threading.Lock()

    def session_fingerprint(self):
        return self.fp

    def get(self, url, headers=None, timeout=20):
        with self.lock:
            self.calls.append(("GET", url))
            if self.get_script:
                return self.get_script.pop(0)
        return {"kind": "response", "status": 200, "text": "<html>index</html>"}

    def post_form(self, url, form=None, headers=None, timeout=20):
        with self.lock:
            self.calls.append(("POST", url))
            if not self.script:
                raise AssertionError("响应脚本为空：%s" % url)
            return self.script.pop(0)

    def gets(self, gnm=None):
        return [u for m, u in self.calls
                if m == "GET" and (gnm is None or ("gnmkdm=" + gnm) in u)]

    def posts(self):
        return [u for m, u in self.calls if m == "POST"]


def _call(t, gnm=GRADE_GNM, path=GRADE_PATH, index=GRADE_INDEX_PATH):
    return _fetch_paged(t, path, gnm, index,
                        {"xnm": "2025", "xqm": "3"}, "items")


class ActivationMemoTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ,
                                   {activation.ENV_FLAG: "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        activation.LEDGER.clear()
        self.addCleanup(activation.LEDGER.clear)

    def test_second_same_code_skips_get(self):
        t = _FakeTransport([_ok(), _ok()])
        self.assertEqual(_call(t).outcome, "ok")
        self.assertEqual(_call(t).outcome, "ok")
        self.assertEqual(len(t.gets(GRADE_GNM)), 1)
        self.assertEqual(len(t.posts()), 2)

    def test_cross_code_each_activates_once(self):
        t = _FakeTransport([_ok(), _ok(), _ok()])
        self.assertEqual(_call(t).outcome, "ok")
        self.assertEqual(_call(t, gnm=EXAM_GNM, path=EXAM_PATH,
                               index=EXAM_INDEX_PATH).outcome, "ok")
        self.assertEqual(_call(t).outcome, "ok")
        self.assertEqual(len(t.gets(GRADE_GNM)), 1)
        self.assertEqual(len(t.gets(EXAM_GNM)), 1)
        self.assertEqual(len(t.posts()), 3)

    def test_cookie_rotation_forces_reactivation(self):
        t = _FakeTransport([_ok(), _ok()])
        _call(t)
        t.fp = "fp-2"
        _call(t)
        self.assertEqual(len(t.gets(GRADE_GNM)), 2)

    def test_code_910_retries_exactly_once(self):
        t = _FakeTransport([_ok()])
        _call(t)
        before = len(t.calls)
        t.script = [_code_910(), _ok()]
        out = _call(t)
        self.assertEqual(out.outcome, "ok")
        delta = t.calls[before:]
        self.assertEqual([m for m, _ in delta], ["POST", "GET", "POST"])
        self.assertEqual(len([1 for m, _ in delta if m == "GET"]), 1)

    def test_empty_after_skip_is_reverified(self):
        t = _FakeTransport([_ok()])
        _call(t)
        t.script = [_empty(), _empty()]
        out = _call(t)
        self.assertEqual(out.outcome, "ok_empty")
        self.assertIn("重新激活核验", out.diag)
        # 首次 1 次 + 空结果核验 1 次
        self.assertEqual(len(t.gets(GRADE_GNM)), 2)

    def test_login_html_does_not_mark_active(self):
        t = _FakeTransport([_login_html(), _ok()])
        self.assertEqual(_call(t).outcome, "session_expired")
        self.assertEqual(_call(t).outcome, "ok")
        self.assertEqual(len(t.gets(GRADE_GNM)), 2)

    def test_switch_off_always_activates(self):
        with mock.patch.dict(os.environ, {activation.ENV_FLAG: "0"}):
            t = _FakeTransport([_ok(), _ok()])
            _call(t)
            _call(t)
        self.assertEqual(len(t.gets(GRADE_GNM)), 2)

    def test_concurrent_same_key_single_get(self):
        t = _FakeTransport([_ok() for _ in range(4)])
        barrier = threading.Barrier(4)

        def run():
            barrier.wait()
            return _call(t).outcome

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: run(), range(4)))
        self.assertEqual(results, ["ok"] * 4)
        self.assertEqual(len(t.gets(GRADE_GNM)), 1)
        self.assertEqual(len(t.posts()), 4)


class RawBusinessCodeTests(unittest.TestCase):
    def test_narrow_cases(self):
        self.assertEqual(raw_business_code('{"code": 910}'), 910)
        self.assertEqual(raw_business_code('{"code": 200}'), 200)
        self.assertIsNone(raw_business_code('{"code": "910"}'))
        self.assertIsNone(raw_business_code('{"code": true}'))
        self.assertIsNone(raw_business_code('{"code": 910.0}'))
        self.assertIsNone(raw_business_code("<html>登录</html>"))
        self.assertIsNone(raw_business_code("not json"))
        self.assertIsNone(raw_business_code(""))
        self.assertIsNone(raw_business_code("[1,2]"))


class ActivationLedgerTests(unittest.TestCase):
    def test_ttl_expiry(self):
        now = {"t": 100.0}
        ledger = activation.ActivationLedger(ttl_seconds=30,
                                             clock=lambda: now["t"])
        key = activation.ActivationKey("h", "N1", "fp")
        ledger.mark(key)
        now["t"] = 129
        self.assertTrue(ledger.is_active(key))
        now["t"] = 131
        self.assertFalse(ledger.is_active(key))

    def test_capacity_evicts_oldest(self):
        ledger = activation.ActivationLedger(max_entries=2,
                                             clock=lambda: 0.0)
        keys = [activation.ActivationKey("h", "N%d" % i, "fp") for i in range(3)]
        for k in keys:
            ledger.mark(k)
        self.assertFalse(ledger.is_active(keys[0]))
        self.assertTrue(ledger.is_active(keys[1]))
        self.assertTrue(ledger.is_active(keys[2]))

    def test_invalidate_and_clear(self):
        ledger = activation.ActivationLedger(clock=lambda: 0.0)
        key = activation.ActivationKey("h", "N1", "fp")
        ledger.mark(key)
        ledger.invalidate(key)
        self.assertFalse(ledger.is_active(key))
        ledger.mark(key)
        ledger.clear()
        self.assertFalse(ledger.is_active(key))


class ActivationGateTests(unittest.TestCase):
    """§20.3：激活 GET 失败必须终止空结果核验/910 重试，保留旧成功数据。"""

    def setUp(self):
        self.env = mock.patch.dict(os.environ,
                                   {activation.ENV_FLAG: "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        activation.LEDGER.clear()
        self.addCleanup(activation.LEDGER.clear)

    def test_retry_get_network_error_aborts_empty_verify(self):
        t = _FakeTransport([_ok()])
        self.assertEqual(_call(t).outcome, "ok")
        t.script = [_empty()]
        t.get_script = [{"kind": "network_error", "err": "synthetic"}]
        out = _call(t)
        self.assertEqual(out.outcome, "network_error")
        self.assertEqual(len(t.gets(GRADE_GNM)), 2)   # 首次激活 + 补激活
        self.assertEqual(len(t.posts()), 2)           # 未做第二次空 POST 之外的请求

    def test_retry_get_http_error_aborts(self):
        t = _FakeTransport([_ok()])
        _call(t)
        t.script = [_empty()]
        t.get_script = [{"kind": "response", "status": 500, "text": "<html/>"}]
        self.assertEqual(_call(t).outcome, "http_error")

    def test_retry_get_login_page_aborts(self):
        t = _FakeTransport([_ok()])
        _call(t)
        t.script = [_empty()]
        t.get_script = [_login_html()]
        self.assertEqual(_call(t).outcome, "session_expired")

    def test_http500_with_910_body_keeps_http_semantics(self):
        t = _FakeTransport([_ok()])
        _call(t)
        before = len(t.calls)
        t.script = [{"kind": "response", "status": 500,
                     "text": json.dumps({"code": 910, "items": []})}]
        out = _call(t)
        self.assertEqual(out.outcome, "http_error")
        delta = t.calls[before:]
        self.assertEqual([m for m, _ in delta], ["POST"])   # 不扩大为补激活重试

    def test_legit_empty_without_old_snapshot_still_ok_empty(self):
        t = _FakeTransport([_empty()])
        out = _call(t)
        self.assertEqual(out.outcome, "ok_empty")
        self.assertEqual(len(t.gets(GRADE_GNM)), 1)

    def test_timetable_retry_get_failure_aborts(self):
        from src.adapters.campus.jwweb import fetch_timetable_web
        t = _FakeTransport([{"kind": "response", "status": 200,
                             "text": json.dumps({"kbList": [{"kcmc": "a"}]})}])
        self.assertEqual(fetch_timetable_web(t, "2026", "3").outcome, "ok")
        t.script = [{"kind": "response", "status": 200,
                     "text": json.dumps({"kbList": []})}]
        t.get_script = [{"kind": "network_error", "err": "synthetic"}]
        out = fetch_timetable_web(t, "2026", "3")
        self.assertEqual(out.outcome, "network_error")

    def test_session_lock_serializes_codes_in_order(self):
        t = _FakeTransport([_ok(), _ok()])
        self.assertEqual(_call(t).outcome, "ok")
        self.assertEqual(_call(t, gnm=EXAM_GNM, path=EXAM_PATH,
                               index=EXAM_INDEX_PATH).outcome, "ok")
        seq = [m for m, _ in t.calls]
        self.assertEqual(seq, ["GET", "POST", "GET", "POST"])

    def test_session_lock_survives_ledger_eviction(self):
        ledger = activation.ActivationLedger(max_entries=1, clock=lambda: 0.0)
        lock1 = ledger.session_guard("fp-x")
        for i in range(40):
            ledger.mark(activation.ActivationKey("h", "N%d" % i, "fp-x"))
        self.assertIs(ledger.session_guard("fp-x"), lock1)


class ActivationGateServiceTests(unittest.TestCase):
    """完整 get_grades 服务路径：补激活失败 → partial 保旧，快照不被空结果覆盖。"""

    def setUp(self):
        self.env = mock.patch.dict(os.environ,
                                   {activation.ENV_FLAG: "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        activation.LEDGER.clear()
        self.addCleanup(activation.LEDGER.clear)

    def test_failed_reactivation_keeps_old_grades_snapshot(self):
        from test.helpers import seed_session, temp_dir
        from src.auth import session as S
        from src.service.jwweb_service import JWWebService
        from src.storage.store import SnapshotStore
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        seed_session(d, "acct-gate")
        sess = S.load_session(d, "acct-gate") or {}
        sess["jwglxt_cookies"] = [{"name": "JSESSIONID", "value": "v",
                                   "domain": "jwxt.zjgsu.edu.cn", "path": "/"}]
        S.save_session(d, "acct-gate", sess)

        class _SvcT:
            def __init__(self):
                self.gets = []
                self.posts = []
                self.mode = "ok"
                self.get_fail = False

            def session_fingerprint(self):
                return "svc-fp"

            def set_cookies(self, cookies):
                assert isinstance(cookies, list) and cookies

            def export_cookies(self):
                return [{"name": "JSESSIONID", "value": "v",
                         "domain": "jwxt.zjgsu.edu.cn", "path": "/"}]

            def get(self, url, headers=None, timeout=20):
                self.gets.append(url)
                if self.get_fail:
                    return {"kind": "network_error", "err": "synthetic"}
                return {"kind": "response", "status": 200,
                        "text": "<html>index</html>"}

            def post_form(self, url, form=None, headers=None, timeout=20):
                self.posts.append(url)
                if self.mode == "empty":
                    return _empty()
                return _ok(1)

        fake = _SvcT()
        svc = JWWebService(SnapshotStore(d / "cache.sqlite3"), d,
                           jwweb_transport=fake)
        r1 = svc.get_grades("2025", "3")
        self.assertEqual(r1["status"], "ok")
        self.assertEqual(len(r1["items"]), 1)
        fake.mode = "empty"
        fake.get_fail = True
        fake.gets.clear()
        fake.posts.clear()
        r2 = svc.get_grades("2025", "3")
        self.assertEqual(r2["status"], "partial")
        self.assertEqual(len(r2["items"]), 1)      # 旧成功快照保留
        snap = svc.store.get_snapshot(
            "acct-gate", MODULE_GRADES, SOURCE_JWGLXT, "2025-3")
        self.assertEqual(len(snap["items"]), 1)    # 快照未被空结果覆盖
        self.assertTrue(any("激活" in i.get("safe_message", "")
                            or "network" in i.get("safe_message", "")
                            for i in r2["issues"]))


if __name__ == "__main__":
    unittest.main()
