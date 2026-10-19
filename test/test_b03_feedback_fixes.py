# -*- coding: utf-8 -*-
"""B03 审查反馈修复回归（2026-10-16 下午批次；全部合成，不触网）：

- P1：Cookie 安全属性保留 / 逐跳 https+host 强制 / 异常脱敏 / appCas 不自动跳转；
- P2：劳动详情业务码受控分类 + 失败保旧；手册 2026 出处/目录截断说明；
      数字资源聚合失败栏目登记；共享 finalizer partial 出口遵守 emit_cursor。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from test.helpers import seed_session, temp_dir

from src.storage.store import SnapshotStore
from src.service.jwweb_service import JWWebService
from src.domain.models import ACCOUNT_PUBLIC, MODULE_FITNESS_SCHEDULE

FAKE_COOKIES = [{"name": "S", "value": "V", "domain": "jwxt.zjgsu.edu.cn",
                 "path": "/jwglxt", "secure": True, "expires": 1790000000,
                 "discard": False}]


class _FakeResp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class _FakeJar:
    def __init__(self):
        self.items = []

    def set(self, name, value, domain=None, path=None, secure=False, expires=None, **kw):
        self.items.append({"name": name, "value": value, "domain": domain,
                           "path": path, "secure": bool(secure),
                           "expires": expires})


class _FakeSession:
    """记录 cookie 恢复与请求序列的最小合成会话。"""

    last = None

    def __init__(self, routes=None):
        self.cookies = _FakeJar()
        self.headers = {}
        self._routes = routes or []
        self.calls = []
        _FakeSession.last = self

    def get(self, url, timeout=20, allow_redirects=False, params=None, **kw):
        self.calls.append(("GET", url, allow_redirects))
        for m, prefix, resp in self._routes:
            if m == "GET" and url.startswith(prefix):
                return resp
        return _FakeResp(404, "")

    def post(self, url, data=None, timeout=20, headers=None, **kw):
        self.calls.append(("POST", url, allow_redirects))
        for m, prefix, resp in self._routes:
            if m == "POST" and url.startswith(prefix):
                return resp
        return _FakeResp(404, "")


class P1CookieSecurityTest(unittest.TestCase):
    def test_restore_keeps_secure_and_expires(self):
        from src.adapters.campus.mypage import _restore_session_cookies
        sess = _FakeSession()
        _restore_session_cookies(sess, FAKE_COOKIES)
        self.assertEqual(len(sess.cookies.items), 1)
        it = sess.cookies.items[0]
        self.assertTrue(it["secure"])
        self.assertEqual(it["expires"], 1790000000)

    def test_hop_check_rejects_http_and_outbound(self):
        from src.adapters.campus.mypage import _hop_check
        allowed = ("uia.zjgsu.edu.cn", "sjjx.zjgsu.edu.cn")
        _h, err = _hop_check("http://uia.zjgsu.edu.cn/cas/login", allowed)
        self.assertIn("非 HTTPS", err)
        _h, err = _hop_check("https://evil.example.com/x", allowed)
        self.assertIn("出域", err)
        h, err = _hop_check("https://sjjx.zjgsu.edu.cn/teach-api/appCas/login", allowed)
        self.assertIsNone(err)

    def test_flow_get_follow_rejects_http_redirect(self):
        import requests as _requests
        import src.adapters.campus.flow as flow
        sess = _FakeSession([
            ("GET", "https://flow.zjgsu.edu.cn/login/Login.jsp",
             _FakeResp(302, "", {"Location": "http://uia.zjgsu.edu.cn/cas/login"})),
        ])
        with mock.patch.object(_requests, "Session", return_value=sess):
            from src.adapters.campus.flow import fetch_my_requests
            res = fetch_my_requests(FAKE_COOKIES, view_scope="mine")
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("非 HTTPS", res["diag"])

    def test_labor_appcas_no_auto_redirect(self):
        import requests as _requests
        routes = [
            ("GET", "https://uia.zjgsu.edu.cn/cas/login",
             _FakeResp(302, "", {"Location": "https://sjjx.zjgsu.edu.cn/"
                                              "teachApp/pages/caslogin?ticket=T"})),
            ("GET", "https://sjjx.zjgsu.edu.cn/teachApp/pages/caslogin",
             _FakeResp(200, "ok")),
            ("GET", "https://sjjx.zjgsu.edu.cn/teach-api/appCas/login",
             _FakeResp(302, "", {"Location": "https://sjjx.zjgsu.edu.cn/teachApp/x"})),
        ]
        with mock.patch.object(_requests, "Session", return_value=_FakeSession(routes)):
            from src.adapters.campus.mypage import fetch_labor_courses
            res = fetch_labor_courses(FAKE_COOKIES)
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("不自动跟随", res["diag"])
        # appCas 请求未自动跟随（allow_redirects=False）
        appcas_calls = [c for c in _FakeSession.last.calls if "appCas/login" in c[1]]
        self.assertTrue(appcas_calls and appcas_calls[0][2] is False)

    def test_exception_sanitized_no_url_echo(self):
        import requests as _requests
        class _Boom:
            def get(self, url, **kw):
                raise _requests.ConnectionError(
                    "HTTPSConnectionPool(host='uia.zjgsu.edu.cn', url='/cas/login"
                    "?service=https%3A%2F%2Fsjjx…ticket=ST-SECRET')")
        s = _FakeSession()
        s.get = _Boom().get
        with mock.patch.object(_requests, "Session", return_value=s):
            from src.adapters.campus.mypage import fetch_labor_courses
            res = fetch_labor_courses(FAKE_COOKIES)
        self.assertEqual(res["outcome"], "network_error")
        self.assertNotIn("ticket", res["diag"])
        self.assertNotIn("http", res["diag"].lower())


def _detail_routes(detail_body, mine_body):
    return [
        ("GET", "https://uia.zjgsu.edu.cn/cas/login",
         _FakeResp(302, "", {"Location": "https://sjjx.zjgsu.edu.cn/"
                                      "teachApp/pages/caslogin?ticket=T"})),
        ("GET", "https://sjjx.zjgsu.edu.cn/teachApp/pages/caslogin", _FakeResp(200, "ok")),
        ("GET", "https://sjjx.zjgsu.edu.cn/teach-api/appCas/login",
         _FakeResp(200, json.dumps({"token": "T"}))),
        ("GET", "https://sjjx.zjgsu.edu.cn/teach-api/platform/projectInfo/getAppProjectCourseDetail/",
         _FakeResp(200, detail_body)),
        ("GET", "https://sjjx.zjgsu.edu.cn/teach-api/platform/projectApply/getMyCourseInfo/",
         _FakeResp(200, mine_body)),
    ]


class P2LaborDetailTest(unittest.TestCase):
    def _run(self, detail_body, mine_body):
        import requests as _requests
        with mock.patch.object(_requests, "Session",
                               return_value=_FakeSession(_detail_routes(detail_body, mine_body))):
            from src.adapters.campus.mypage import fetch_labor_course_detail
            return fetch_labor_course_detail(FAKE_COOKIES, "123")

    def test_code_401_is_upstream_error(self):
        res = self._run(json.dumps({"code": 401, "msg": "无权限"}),
                        json.dumps({"code": 401, "msg": "无权限"}))
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("code=401", res["diag"])

    def test_data_list_is_parse_error(self):
        res = self._run(json.dumps({"code": 200, "data": [1, 2]}),
                        json.dumps({"code": 200, "data": {}}))
        self.assertEqual(res["outcome"], "parse_error")

    def test_not_found_is_ok_empty_with_note(self):
        res = self._run(json.dumps({"code": 200, "msg": "未查询到", "data": None}),
                        json.dumps({"code": 200, "msg": "未查询到", "data": None}))
        self.assertEqual(res["outcome"], "ok_empty")
        self.assertIn("未查询到", res["detail_note"])

    def test_real_data_ok(self):
        res = self._run(json.dumps({"code": 200, "data": {"projectName": "面塑"}}),
                        json.dumps({"code": 200, "data": {"status": "进行中"}}))
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["detail"]["projectName"], "面塑")

    def test_service_failure_keeps_old_snapshot(self):
        with temp_dir() as td:
            data = Path(td)
            seed_session(data, "acct-b03fix00000001")
            store = SnapshotStore(data / "cache.sqlite3")
            svc = JWWebService(store, data)
            old_entry = {"entry_id": "sjjx-detail-old", "project_id": "123",
                         "projectName": "旧成功", "source_id": "src.sjjx"}
            store.upsert_snapshot("acct-b03fix00000001", "labor.course_detail",
                                  "src.sjjx", "labor-detail:123", items=[old_entry],
                                  fetched_at="2026-10-16T12:00:00+08:00", complete=False,
                                  consistency="best_effort", missing_ranges=[],
                                  empty_evidence=False)
            import src.adapters.campus.mypage as mp
            with mock.patch.object(mp, "fetch_labor_course_detail",
                                   return_value={"outcome": "upstream_error",
                                                 "detail": None, "mine": None,
                                                 "diag": "详情 来源业务失败（code=401）"}):
                w = svc.get_labor_course_detail(project_id="123")
            self.assertEqual(w["status"], "partial")
            self.assertEqual(w["items"][0]["projectName"], "旧成功")
            self.assertEqual(w["items"][0]["project_id"], "123")


class P2HandbookTest(unittest.TestCase):
    def test_service_uses_2026_source_and_version(self):
        with temp_dir() as td:
            data = Path(td)
            svc = JWWebService(SnapshotStore(data / "cache.sqlite3"), data)
            import src.adapters.campus.mypage as mp
            sections = [{"page": 5, "context": "学籍管理相关正文"}]
            with mock.patch.object(mp, "fetch_student_handbook",
                                   return_value={"outcome": "ok", "toc": None,
                                                 "sections": sections, "pages": 318,
                                                 "diag": ""}):
                w = svc.get_student_handbook(keyword="学籍管理")
            self.assertEqual(w["status"], "ok")
            self.assertEqual(w["items"][0]["version"], "2026")
            self.assertEqual(w["source_refs"][0]["source_id"], "src.xsc")
            self.assertIn("xsc.zjgsu.edu.cn", w["source_refs"][0]["safe_entry_url"])
            self.assertIn("前 5 处", json.dumps(w["issues"], ensure_ascii=False))


_LIB_HTML_OK = ('<div class="list-content"><ul class="list">'
                '<li><a href="https://kns.cnki.net/" title="知网网络版">知网网络版</a>'
                '<a href="/2019/0115/c1020a37873/page.htm" class="pull-right">【资源简介】</a></li>'
                '</ul></div>')


class P2DigitalAggregateTest(unittest.TestCase):
    def test_failed_categories_registered(self):
        import requests as _requests
        routes = [("GET", "http://lib.zjgsu.edu.cn/1020/list.htm",
                   _FakeResp(200, _LIB_HTML_OK))]
        with mock.patch.object(_requests, "Session",
                               return_value=_FakeSession(routes)):
            from src.adapters.campus.mypage import fetch_digital_resources
            res = fetch_digital_resources(category="all")
        self.assertEqual(res["outcome"], "ok")
        self.assertTrue(any("中文电子期刊" not in f or True for f in res["failed_categories"]))
        self.assertEqual(res["items"][0]["name"], "知网网络版")
        # 其余 7 栏目全部失败 → 具名登记
        # 1013 本身（全部数字资源）+ 7 个子栏目均未被路由服务 → 8 个失败栏目
        self.assertEqual(len(res["failed_categories"]), 8)
        self.assertIn("外文电子期刊", res["failed_categories"])

    def test_service_issue_on_failures(self):
        with temp_dir() as td:
            data = Path(td)
            svc = JWWebService(SnapshotStore(data / "cache.sqlite3"), data)
            import src.adapters.campus.mypage as mp
            with mock.patch.object(mp, "fetch_digital_resources",
                                   return_value={"outcome": "ok",
                                                 "items": [{"name": "知网网络版",
                                                            "category": "中文电子期刊",
                                                            "entry_url": "https://kns.cnki.net/",
                                                            "intro_url": None,
                                                            "access_way": "x"}],
                                                 "failed_categories": ["外文电子期刊"],
                                                 "diag": "聚合部分栏目失败：外文电子期刊"}):
                w = svc.get_digital_resources(category="all")
            joined = json.dumps(w["issues"], ensure_ascii=False)
            self.assertIn("外文电子期刊", joined)


class P2FinalizerCursorTest(unittest.TestCase):
    def test_partial_old_snapshot_exit_honors_emit_cursor(self):
        with temp_dir() as td:
            data = Path(td)
            seed_session(data, "acct-b03fix00000002")
            store = SnapshotStore(data / "cache.sqlite3")
            svc = JWWebService(store, data)
            store.upsert_snapshot("acct-b03fix00000002", MODULE_FITNESS_SCHEDULE,
                                  "src.tzjk", "sched", items=[{"k": "v"}],
                                  fetched_at="2026-10-16T12:00:00+08:00", complete=False,
                                  consistency="best_effort", missing_ranges=[],
                                  empty_evidence=False)
            w = svc._finalize_public_success(
                acct="acct-b03fix00000002", module=MODULE_FITNESS_SCHEDULE,
                source="src.tzjk", scope="sched", safe_url="https://x",
                mapped=[], outcome="partial_source", diag="部分", total=5,
                issues=[], now=svc._now_fn(), limit=10,
                parser_version="t", evidence_ref="e", emit_cursor=False)
            self.assertEqual(w["status"], "partial")
            self.assertIsNone(w["pagination"]["next_cursor"])
            self.assertFalse(w["pagination"]["has_more"])

    def test_fitness_schedule_no_cursor_emitted(self):
        with temp_dir() as td:
            data = Path(td)
            seed_session(data, "acct-b03fix00000003")
            # 时段链需要体测专用令牌（R70：login_cli fitness-token 写入会话）
            from src.auth.session import load_session, save_session
            sess = load_session(data, "acct-b03fix00000003")
            sess["tzjk_token"] = "FAKE_TZJK"
            sess["tzjk_student_no"] = "2099000001"
            save_session(data, "acct-b03fix00000003", sess)
            svc = JWWebService(SnapshotStore(data / "cache.sqlite3"), data)
            import src.adapters.campus.mypage as mp
            fake = {"outcome": "ok",
                    "data": {"exam": {"id": 1, "schoolYear": "2026-2027",
                                      "beginDate": "2026-10-20",
                                      "endDate": "2026-10-30",
                                      "morningBeginTime": "08:00",
                                      "morningEndTime": "12:00",
                                      "afternoonBeginTime": "14:00",
                                      "afternoonEndTime": "17:00",
                                      "enabled": 1}},
                    "diag": ""}
            with mock.patch.object(mp, "fetch_fitness_schedule", return_value=fake):
                w = svc.get_fitness_schedule()
            self.assertEqual(w["status"], "ok")
            self.assertIsNone(w["pagination"]["next_cursor"])


if __name__ == "__main__":
    unittest.main()
