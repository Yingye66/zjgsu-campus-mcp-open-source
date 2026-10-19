# -*- coding: utf-8 -*-
"""T1-T3 工具合成测试（2026-10-16 批次；不触网）：

- flow 适配器（F11）：CAS/Login.jsp 链、sessionkey 两段式、白名单投影、
  失败分类（登录态未建立→upstream_error；出域→upstream_error；坏结构→parse_error）；
- xsc 公示（F14）：列表解析/keyword/翻页超界 404→合法空/详情+附件/出域拒绝；
- 数字资源（F26）：条目+简介配对/内网 access_way/未知 category；
- 服务层：unavailable 语义、public 域快照、emit_cursor=False、写操作不做 issue；
- 全部为合成响应（不发送任何真实请求、不复制真实凭据/私人数据）。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from test.helpers import temp_dir

from src.storage.store import SnapshotStore
from src.service.jwweb_service import JWWebService
from src.domain.models import ACCOUNT_PUBLIC

FAKE_COOKIES = [{"name": "JSESSIONID", "value": "FAKE", "domain": "jwxt.zjgsu.edu.cn",
                 "path": "/jwglxt", "secure": False, "expires": None, "discard": True}]


class _FakeResp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class _FakeSession:
    """按 URL 规则路由的合成会话（记录调用；cookie 集合可控）。"""

    def __init__(self, routes, cookie_names=()):
        self._routes = routes          # list[(method, prefix, resp)]
        self._cookie_names = list(cookie_names)
        self.cookies = _CookieJar(self._cookie_names)
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=20, allow_redirects=False, params=None, **kw):
        self.calls.append(("GET", url, params))
        for m, prefix, resp in self._routes:
            if m == "GET" and url.startswith(prefix):
                return resp
        return _FakeResp(404, "")

    def post(self, url, data=None, timeout=20, headers=None, **kw):
        self.calls.append(("POST", url, data))
        for m, prefix, resp in self._routes:
            if m == "POST" and url.startswith(prefix):
                return resp
        return _FakeResp(404, "")

    def set(self, name, value, domain=None, path=None, **kw):
        if name not in self._cookie_names:
            self._cookie_names.append(name)


class _CookieJar(list):
    def __init__(self, names):
        super().__init__(names)

    def __iter__(self):
        return iter(_C(name) for name in super().__iter__())


class _C:
    def __init__(self, name, domain="flow.zjgsu.edu.cn"):
        self.name = name
        self.domain = domain


def _flow_routes(rows, sk="SK123"):
    login_html = "<html>ok</html>"
    return [
        ("GET", "https://flow.zjgsu.edu.cn/login/Login.jsp", _FakeResp(200, login_html)),
        ("GET", "https://uia.zjgsu.edu.cn/cas/login", _FakeResp(200, login_html)),
        ("GET", "https://flow.zjgsu.edu.cn/api/workflow/mobile/getListResult",
         _FakeResp(200, json.dumps({"datas": sk, "status": True}))),
        ("POST", "https://flow.zjgsu.edu.cn/api/ec/dev/table/datas",
         _FakeResp(200, json.dumps({"datas": rows, "columns": []}))),
    ]


def _row(rid="369001", name="示例流程-张三-2026-10-01", date="2026-10-01", wf="251"):
    return {"requestid": rid, "requestname": name, "createdate": date, "workflowid": wf,
            "appurl": rid, "randomField1span": "span-noise", "userid": "111"}


class FlowAdapterTest(unittest.TestCase):
    def _run(self, rows, cookie_names=("loginidweaver",), routes=None):
        import requests as _requests
        sess = _FakeSession(routes or _flow_routes(rows), cookie_names)
        with mock.patch.object(_requests, "Session", return_value=sess):
            from src.adapters.campus.flow import fetch_my_requests
            return fetch_my_requests(FAKE_COOKIES, view_scope="mine"), sess

    def test_ok_projection_whitelist(self):
        res, sess = self._run([_row(), _row(rid="369002")])
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(len(res["items"]), 2)
        e = res["items"][0]
        self.assertEqual(e["request_id"], "369001")
        self.assertIn("示例流程", e["request_name"])
        self.assertEqual(e["create_date"], "2026-10-01")
        self.assertEqual(e["workflow_id"], "251")
        # 未知/span 噪声字段不得透传
        self.assertNotIn("appurl", json.dumps(res))
        self.assertNotIn("randomField1span", json.dumps(res))
        # 调用序列：登录链 → getListResult → table/datas
        self.assertTrue(any("getListResult" in c[1] for c in sess.calls))
        self.assertTrue(any("table/datas" in c[1] for c in sess.calls))

    def test_ok_empty(self):
        res, _ = self._run([])
        self.assertEqual(res["outcome"], "ok_empty")
        self.assertEqual(res["items"], [])

    def test_login_state_not_established(self):
        res, _ = self._run([_row()], cookie_names=("ecology",))
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("登录态未建立", res["diag"])

    def test_redirect_host_out_of_domain(self):
        routes = [("GET", "https://flow.zjgsu.edu.cn/login/Login.jsp",
                   _FakeResp(302, "", {"Location": "https://evil.example.com/x"}))]
        res, _ = self._run([_row()], routes=routes)
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("出域", res["diag"])

    def test_datas_bad_structure_parse_error(self):
        routes = _flow_routes(None)
        routes[3] = ("POST", "https://flow.zjgsu.edu.cn/api/ec/dev/table/datas",
                     _FakeResp(200, json.dumps({"datas": "not-a-list"})))
        res, _ = self._run([_row()], routes=routes)
        self.assertEqual(res["outcome"], "parse_error")

    def test_rows_missing_primary_key_counted(self):
        res, _ = self._run([_row(rid=""), _row(rid="369009")])
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["bad_rows"], 1)

    def test_unknown_scope_rejected(self):
        from src.adapters.campus.flow import fetch_my_requests
        res = fetch_my_requests(FAKE_COOKIES, view_scope="admin")
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("viewScope", res["diag"])


_LIST_HTML = """
<html><body>
<div class="wp_article_list">
<ul class="news_list list2">
<li><a href="/2026/1015/c1827a232808/page.htm" title="2026年度港澳及华侨学生奖学金评审结果公示">2026年度港澳及华侨学生奖学金评审结果公示</a><span class="fr">2026-10-15</span></li>
<li><a href="/2026/1012/c1827a232695/page.htm" title="2025-2026学年本科学生国家奖学金拟推荐获奖名单公示">国家奖学金拟推荐获奖名单公示</a><span class="fr">2026-10-12</span></li>
<li><a href="/2026/0929/c1827a232186/page.htm">关于2027年春季学期免学费交换项目学生选拔总成绩公示</a><span class="fr">2026-09-29</span></li>
</ul></div></body></html>
"""

_DETAIL_HTML = """
<html><body>
<div class="wp_article_content"><p>经学院推荐、学校评审，现将获奖名单公示如下，公示期为2026年10月12日至10月20日。</p>
<p><a href="/_upload/article/files/aa/bb/list.pdf?v=1">公示名单.pdf</a></p></div>
<div class="wp_article_footer">相关链接</div>
</body></html>
"""

_LIB_HTML = """
<html><body>
<div class="quick-access"><ul>
<li><a>校外入口</a></li>
<li><a href="https://ds.carsi.edu.cn/ds/index.html">CARSI认证</a></li>
<li><a href="https://vpn.zjgsu.edu.cn/">教师VPN</a></li>
</ul></div>
<div class="list-content" frag="窗口12" portletmode="simpleList">
<ul class="list">
<li><a href="http://10.99.253.33/kns55/brief/result.aspx?dbPrefix=CJFq" title="中国学术期刊全文数据库（CNKI中国知网）镜像" target="_blank">中国学术期刊全文数据库（CNKI中国知网）镜像</a><a href="/2019/0115/c1020a37872/page.htm" class="pull-right">【资源简介】</a></li>
<li><a href="https://kns.cnki.net/" title="中国知网网络版" target="_blank">中国知网网络版</a><a href="/2019/0115/c1020a37873/page.htm" class="pull-right">【资源简介】</a></li>
</ul></div>
</body></html>
"""


class XscPublicationsAdapterTest(unittest.TestCase):
    def _run_list(self, html, page=1, keyword=""):
        import src.adapters.campus.mypage as mp
        sess = _FakeSession([("GET", "https://xsc.zjgsu.edu.cn/1827/list",
                              _FakeResp(200, html))])
        with mock.patch.object(mp.requests, "Session", return_value=sess):
            return mp.fetch_scholarship_publications(keyword=keyword, page=page)

    def test_list_parse(self):
        res = self._run_list(_LIST_HTML)
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(len(res["items"]), 3)
        it = res["items"][0]
        self.assertIn("奖学金评审结果公示", it["title"])
        self.assertTrue(it["url"].startswith("https://xsc.zjgsu.edu.cn/2026/"))
        self.assertEqual(it["date"], "2026-10-15")

    def test_keyword_filter(self):
        res = self._run_list(_LIST_HTML, keyword="国家奖学金")
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(len(res["items"]), 1)

    def test_page2_404_is_ok_empty(self):
        import src.adapters.campus.mypage as mp
        sess = _FakeSession([("GET", "https://xsc.zjgsu.edu.cn/1827/list",
                              _FakeResp(404, ""))])
        with mock.patch.object(mp.requests, "Session", return_value=sess):
            res = mp.fetch_scholarship_publications(page=2)
        self.assertEqual(res["outcome"], "ok_empty")

    def test_detail_and_attachments(self):
        import src.adapters.campus.mypage as mp
        sess = _FakeSession([("GET", "https://xsc.zjgsu.edu.cn/2026/1015/c1827a232808/page.htm",
                              _FakeResp(200, _DETAIL_HTML))])
        with mock.patch.object(mp.requests, "Session", return_value=sess):
            res = mp.fetch_scholarship_publications(
                detail_url="https://xsc.zjgsu.edu.cn/2026/1015/c1827a232808/page.htm")
        self.assertEqual(res["outcome"], "ok")
        self.assertIn("名单公示", res["detail"])
        self.assertEqual(len(res["attachments"]), 1)
        self.assertIn("list.pdf", res["attachments"][0]["url"])

    def test_detail_out_of_domain(self):
        import src.adapters.campus.mypage as mp
        res = mp.fetch_scholarship_publications(detail_url="https://evil.example.com/a.htm")
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("出域", res["diag"])

    def test_empty_list_page1_is_parse_error(self):
        res = self._run_list("<html><body>no list</body></html>")
        self.assertEqual(res["outcome"], "parse_error")


class DigitalResourcesAdapterTest(unittest.TestCase):
    def _run(self, html, category="chinese_journal", keyword=""):
        import src.adapters.campus.mypage as mp
        sess = _FakeSession([("GET", "http://lib.zjgsu.edu.cn/1020/list.htm",
                              _FakeResp(200, html))])
        with mock.patch.object(mp.requests, "Session", return_value=sess):
            return mp.fetch_digital_resources(category=category, keyword=keyword)

    def test_entries_and_intro_pairing(self):
        res = self._run(_LIB_HTML)
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(len(res["items"]), 2)
        first = res["items"][0]
        self.assertEqual(first["name"], "中国学术期刊全文数据库（CNKI中国知网）镜像")
        self.assertIn("10.99.253.33", first["entry_url"])
        self.assertEqual(first["access_way"], "校内网/VPN（内网地址）")
        self.assertIn("c1020a37872", first["intro_url"] or "")
        second = res["items"][1]
        self.assertEqual(second["access_way"], "按入口访问（校外可用性以图书馆说明为准）")

    def test_keyword(self):
        res = self._run(_LIB_HTML, keyword="知网网络版")
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["items"][0]["name"], "中国知网网络版")

    def test_unknown_category(self):
        import src.adapters.campus.mypage as mp
        res = mp.fetch_digital_resources(category="nope")
        self.assertEqual(res["outcome"], "upstream_error")

    def test_nav_links_not_entries(self):
        res = self._run(_LIB_HTML)
        names = [it["name"] for it in res["items"]]
        self.assertNotIn("纸质资源", names)
        self.assertNotIn("中文电子期刊", names)


def _svc(data_dir):
    store = SnapshotStore(data_dir / "cache.sqlite3")
    return JWWebService(store, data_dir)


class ServiceLevelTest(unittest.TestCase):
    def test_approvals_success_snapshot_and_no_cursor(self):
        with temp_dir() as td:
            from pathlib import Path
            data = Path(td)
            from test.helpers import seed_session
            seed_session(data, "acct-t12340000000001")
            svc = _svc(data)
            import src.adapters.campus.flow as flow
            with mock.patch.object(flow, "fetch_my_requests",
                                   return_value={"outcome": "ok",
                                                 "items": [{"request_id": "370001",
                                                            "request_name": "示例流程-张三-2026-10-01",
                                                            "create_date": "2026-10-01",
                                                            "workflow_id": "251"}],
                                                 "bad_rows": 0, "diag": ""}):
                w = svc.get_my_approvals(scope="mine")
            self.assertEqual(w["status"], "ok")
            self.assertEqual(w["module_id"], "approvals.mine")
            self.assertEqual(w["items"][0]["request_id"], "370001")
            self.assertEqual(w["items"][0]["view_scope"], "mine")
            # emit_cursor=False：不发游标
            self.assertIsNone(w["pagination"]["next_cursor"])
            self.assertFalse(w["pagination"]["has_more"])
            # 快照在账号域（不是 public）
            snap = w["coverage"]["source_snapshots"][0]
            self.assertTrue(snap["snapshot_id"])
            # issue 说明写操作不做
            joined = json.dumps(w["issues"], ensure_ascii=False)
            self.assertIn("写操作不做", joined)

    def test_approvals_auth_required_mapping(self):
        with temp_dir() as td:
            from pathlib import Path
            data = Path(td)
            from test.helpers import seed_session
            seed_session(data, "acct-t12340000000002")
            svc = _svc(data)
            import src.adapters.campus.flow as flow
            with mock.patch.object(flow, "fetch_my_requests",
                                   return_value={"outcome": "upstream_error", "items": None,
                                                 "diag": "flow 登录态未建立（CAS 未放行/TGT 失效）"}):
                w = svc.get_my_approvals(scope="mine")
            self.assertEqual(w["status"], "unavailable")
            self.assertEqual(w["issues"][0]["code"], "auth_required")
            self.assertIn("login_cli init", w["issues"][0]["safe_message"])

    def test_approvals_invalid_scope(self):
        with temp_dir() as td:
            svc = _svc(Path(td))
            w = svc.get_my_approvals(scope="admin")
            self.assertEqual(w["status"], "unavailable")
            self.assertEqual(w["issues"][0]["code"], "invalid_arguments")

    def test_approvals_no_session(self):
        with temp_dir() as td:
            svc = _svc(Path(td))
            w = svc.get_my_approvals()
            self.assertEqual(w["status"], "unavailable")
            self.assertEqual(w["issues"][0]["code"], "auth_required")

    def test_publications_success_public_domain(self):
        with temp_dir() as td:
            data = Path(td)
            svc = _svc(data)
            import src.adapters.campus.mypage as mp
            items = [{"title": "2026 年度专项奖学金评审结果公示",
                      "url": "https://xsc.zjgsu.edu.cn/2026/0527/c1827a223540/page.htm",
                      "date": "2026-05-27"}]
            with mock.patch.object(mp, "fetch_scholarship_publications",
                                   return_value={"outcome": "ok", "items": items,
                                                 "detail": None, "attachments": None, "diag": ""}):
                w = svc.get_scholarship_publications(keyword="奖学金")
            self.assertEqual(w["status"], "ok")
            self.assertEqual(w["module_id"], "awards.publications")
            self.assertEqual(w["items"][0]["publish_date"], "2026-05-27")
            self.assertIsNone(w["pagination"]["next_cursor"])
            # 公共域快照
            self.assertTrue(w["coverage"]["source_snapshots"][0]["snapshot_id"])

    def test_digital_resources_invalid_category(self):
        with temp_dir() as td:
            svc = _svc(Path(td))
            w = svc.get_digital_resources(category="weixin")
            self.assertEqual(w["status"], "unavailable")
            self.assertEqual(w["issues"][0]["code"], "invalid_arguments")

    def test_digital_resources_success(self):
        with temp_dir() as td:
            data = Path(td)
            svc = _svc(data)
            import src.adapters.campus.mypage as mp
            items = [{"name": "中国知网网络版", "category": "中文电子期刊",
                      "entry_url": "https://kns.cnki.net/", "intro_url": None,
                      "access_way": "按入口访问（校外可用性以图书馆说明为准）"}]
            with mock.patch.object(mp, "fetch_digital_resources",
                                   return_value={"outcome": "ok", "items": items, "diag": ""}):
                w = svc.get_digital_resources(category="chinese_journal")
            self.assertEqual(w["status"], "ok")
            self.assertEqual(w["module_id"], "library.digital")
            self.assertEqual(w["items"][0]["name"], "中国知网网络版")
            self.assertIn("不推断", json.dumps(w["issues"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
