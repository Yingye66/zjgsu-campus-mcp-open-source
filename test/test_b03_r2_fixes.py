# -*- coding: utf-8 -*-
"""B03 r2 复验反馈修复回归（2026-10-16 下午第二批；全部合成，不触网）：

- ③ 端口白名单（8443 拒绝）+ flow/综测后续请求禁自动跳转；
- ① 数字资源：UnboundLocalError 修复 / keyword 无命中=ok_empty / HTTP200 无正文区=parse_error
  / 聚合部分失败→服务层 partial 保旧；
- ② 公示详情：无正文区=parse_error / 正文区限定+附件 urljoin·引号兼容·query·userinfo 净化
  / 详情失败→服务层保旧；
- ⑤ emit_cursor=False 且条目多于 limit → local_truncated 说明。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from test.helpers import seed_session, temp_dir

from src.storage.store import SnapshotStore
from src.service.jwweb_service import JWWebService
from src.domain.models import ACCOUNT_PUBLIC, MODULE_DIGITAL_RESOURCES

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


class _FakeSession:
    last = None

    def __init__(self, routes=None, cookie_names=()):
        self.cookies = _FakeJar()
        for n in cookie_names:
            self.cookies.set(n, "V", domain="flow.zjgsu.edu.cn", path="/")
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


class _FakeJar:
    def __init__(self):
        self.items = []

    def set(self, name, value, domain=None, path=None, secure=False, expires=None, **kw):
        self.items.append({"name": name, "value": value, "domain": domain,
                           "path": path, "secure": bool(secure), "expires": expires})

    def __iter__(self):
        return iter(_CK(i["name"], i["domain"]) for i in list(self.items))


class _CK:
    def __init__(self, name, domain="flow.zjgsu.edu.cn"):
        self.name = name
        self.domain = domain


class PortRestrictionTest(unittest.TestCase):
    def test_hop_check_rejects_8443(self):
        from src.adapters.campus.mypage import _hop_check
        allowed = ("uia.zjgsu.edu.cn", "flow.zjgsu.edu.cn")
        _h, err = _hop_check("https://uia.zjgsu.edu.cn:8443/cas/login", allowed)
        self.assertIn("端口非预期", err)
        h, err = _hop_check("https://flow.zjgsu.edu.cn/login/Login.jsp", allowed)
        self.assertIsNone(err)
        # 默认 443（URL 无显式端口）放行
        h, err = _hop_check("https://uia.zjgsu.edu.cn/cas/login", allowed)
        self.assertIsNone(err)

    def test_flow_hop_rejects_8443(self):
        import requests as _requests
        import src.adapters.campus.flow as flow
        sess = _FakeSession([
            ("GET", "https://flow.zjgsu.edu.cn/login/Login.jsp",
             _FakeResp(302, "", {"Location": "https://uia.zjgsu.edu.cn:8443/cas/login"})),
        ])
        with mock.patch.object(_requests, "Session", return_value=sess):
            res = flow.fetch_my_requests(FAKE_COOKIES, view_scope="mine")
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("端口非预期", res["diag"])


class NoAutoRedirectTest(unittest.TestCase):
    def test_flow_getlistresult_302_controlled(self):
        import requests as _requests
        routes = [
            ("GET", "https://flow.zjgsu.edu.cn/login/Login.jsp", _FakeResp(200, "ok")),
            ("GET", "https://flow.zjgsu.edu.cn/api/workflow/mobile/getListResult",
             _FakeResp(302, "", {"Location": "https://flow.zjgsu.edu.cn/x"})),
        ]
        sess = _FakeSession(routes, cookie_names=("loginidweaver",))
        with mock.patch.object(_requests, "Session", return_value=sess):
            from src.adapters.campus.flow import fetch_my_requests
            res = fetch_my_requests(FAKE_COOKIES, view_scope="mine")
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("不自动跟随", res["diag"])
        self.assertFalse(_FakeSession.last.calls[-1][2])  # allow_redirects=False

    def test_evaluation_result_302_controlled(self):
        import requests as _requests
        routes = [("GET", "https://xszkdh.zjgsu.edu.cn/evaluation-api/",
                   _FakeResp(302, "", {"Location": "https://xszkdh.zjgsu.edu.cn/login"}))]
        with mock.patch.object(_requests, "Session", return_value=_FakeSession(routes)):
            from src.adapters.campus.mypage import fetch_evaluation_result
            res = fetch_evaluation_result(token="T", role="xs")
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("不自动跟随", res["diag"])


_DIGITAL_OK = ('<div class="list-content"><ul class="list">'
               '<li><a href="https://kns.cnki.net/" title="知网网络版">知网网络版</a>'
               '<a href="/2019/0115/c1020a37873/page.htm" class="pull-right">【资源简介】</a></li>'
               '</ul></div>')
_DIGITAL_NO_REGION = "<html><body>服务维护中</body></html>"


class DigitalSemanticsTest(unittest.TestCase):
    def _routes(self, body_1020=_DIGITAL_OK, status_1020=200):
        return [("GET", "http://lib.zjgsu.edu.cn/1020/list.htm",
                 _FakeResp(status_1020, body_1020))]

    def test_keyword_no_hit_is_ok_empty(self):
        import requests as _requests
        with mock.patch.object(_requests, "Session",
                               return_value=_FakeSession(self._routes())):
            from src.adapters.campus.mypage import fetch_digital_resources
            res = fetch_digital_resources(category="chinese_journal", keyword="万方")
        self.assertEqual(res["outcome"], "ok_empty")  # raw>0 过滤后空=合法空
        self.assertEqual(res["raw_total"], 1)

    def test_http200_without_region_is_parse_error(self):
        import requests as _requests
        with mock.patch.object(_requests, "Session",
                               return_value=_FakeSession(self._routes(_DIGITAL_NO_REGION))):
            from src.adapters.campus.mypage import fetch_digital_resources
            res = fetch_digital_resources(category="chinese_journal")
        self.assertEqual(res["outcome"], "parse_error")  # 结构未证≠合法空

    def test_single_category_network_error_no_unbound(self):
        """B03 r2 反馈⑤：单栏目网络异常分支不再触发 UnboundLocalError。"""
        import requests as _requests
        class _BoomSession:
            cookies = _FakeJar()
            headers = {}
            def get(self, url, **kw):
                raise _requests.ConnectionError("boom")
        with mock.patch.object(_requests, "Session", return_value=_BoomSession()):
            from src.adapters.campus.mypage import fetch_digital_resources
            res = fetch_digital_resources(category="chinese_journal")
        self.assertEqual(res["outcome"], "network_error")
        self.assertEqual(res["diag"], "网络错误（ConnectionError）")

    def test_aggregate_partial_service_keeps_old(self):
        with temp_dir() as td:
            data = Path(td)
            seed_session(data, "public")
            store = SnapshotStore(data / "cache.sqlite3")
            svc = JWWebService(store, data)
            store.upsert_snapshot(ACCOUNT_PUBLIC, MODULE_DIGITAL_RESOURCES,
                                  "src.lib", "lib-digital:all:", items=[
                                      {"entry_id": "lib-res-old", "name": "旧条目",
                                       "category": "中文电子期刊",
                                       "entry_url": "https://kns.cnki.net/",
                                       "intro_url": None, "access_way": "x",
                                       "source_id": "src.lib"}],
                                  fetched_at="2026-10-16T12:00:00+08:00", complete=False,
                                  consistency="best_effort", missing_ranges=[],
                                  empty_evidence=False)
            import src.adapters.campus.mypage as mp
            with mock.patch.object(mp, "fetch_digital_resources",
                                   return_value={"outcome": "ok",
                                                 "items": [{"name": "新1条",
                                                            "category": "中文电子期刊",
                                                            "entry_url": "https://x/",
                                                            "intro_url": None,
                                                            "access_way": "x"}],
                                                 "failed_categories": ["外文电子期刊"] * 7,
                                                 "raw_total": 1,
                                                 "diag": "聚合部分栏目失败"}):
                w = svc.get_digital_resources(category="all")
            # 部分栏目失败 → partial + 保旧（8 项旧条目不被 1 项替换）
            self.assertEqual(w["status"], "partial")
            self.assertEqual(w["items"][0]["name"], "旧条目")
            joined = json.dumps(w["issues"], ensure_ascii=False)
            self.assertIn("未入库", joined)


class PublicationDetailBoundaryTest(unittest.TestCase):
    def _detail(self, html):
        import requests as _requests
        routes = [("GET", "https://xsc.zjgsu.edu.cn/2026/1012/c1827a232695/page.htm",
                   _FakeResp(200, html))]
        with mock.patch.object(_requests, "Session", return_value=_FakeSession(routes)):
            from src.adapters.campus.mypage import fetch_scholarship_publications
            return fetch_scholarship_publications(
                detail_url="https://xsc.zjgsu.edu.cn/2026/1012/c1827a232695/page.htm")

    def test_no_body_region_is_parse_error(self):
        # 导航 + 业务错误提示（无 wp_article_content）→ parse_error 不入库
        html = ("<html><body><div class='header'>导航导航导航导航导航导航</div>"
                "<div class='error'>系统繁忙请稍后再试系统繁忙请稍后再试</div></body></html>")
        res = self._detail(html)
        self.assertEqual(res["outcome"], "parse_error")
        self.assertIn("正文区", res["diag"])

    def test_body_region_and_attachment_cleaning(self):
        html = ("<html><body>"
                "<div class='nav'><a href='/1826/list.htm'>工作通知</a>"
                "<a href='javascript:void(0)' class='fr'>更多</a></div>"
                "<div class='wp_article_content'>"
                "<p>经学院推荐、学校评审，现将获奖名单公示如下，公示期至2026年10月20日。</p>"
                "<a href='/_upload/article/files/aa/bb/名单.pdf?v=2'>名单（带query）</a>"
                "<a href='/_upload/article/files/cc/dd/细则.docx'>细则</a>"
                "<a href='https://user:pass@evil.example.com/x.pdf'>外域附件</a>"
                "</div>"
                "<div class='wp_article_footer'><a href='/1098/list.htm'>常见问题附件.pdf</a></div>"
                "</body></html>")
        res = self._detail(html)
        self.assertEqual(res["outcome"], "ok")
        urls = [a["url"] for a in res["attachments"]]
        # query 保留、urljoin 相对地址
        self.assertTrue(any("v=2" in u for u in urls), urls)
        self.assertTrue(any(u.endswith("细则.docx") for u in urls), urls)
        self.assertTrue(all(u.startswith("https://xsc.zjgsu.edu.cn/_upload/") or
                            u == "https://evil.example.com/x.pdf" for u in urls), urls)
        # 正文区外（footer）附件不收入；导航不入正文
        self.assertFalse(any("1098" in u for u in urls))
        # userinfo 净化：外域附件不带 user:pass
        self.assertFalse(any("user:pass" in u for u in urls), urls)
        self.assertNotIn("导航导航", res["detail"])

    def test_service_detail_failure_keeps_old(self):
        with temp_dir() as td:
            data = Path(td)
            seed_session(data, "public")
            store = SnapshotStore(data / "cache.sqlite3")
            svc = JWWebService(store, data)
            url = "https://xsc.zjgsu.edu.cn/2026/1012/c1827a232695/page.htm"
            store.upsert_snapshot(ACCOUNT_PUBLIC, "awards.publications",
                                  "src.xsc", "xsc-pub:%s" % url,
                                  items=[{"entry_id": "xsc-pub-old", "title": "旧公示",
                                          "url": url, "publish_date": "2026-10-12",
                                          "text": "旧正文", "attachments": [],
                                          "source_id": "src.xsc"}],
                                  fetched_at="2026-10-16T12:00:00+08:00", complete=False,
                                  consistency="best_effort", missing_ranges=[],
                                  empty_evidence=False)
            import src.adapters.campus.mypage as mp
            with mock.patch.object(mp, "fetch_scholarship_publications",
                                   return_value={"outcome": "parse_error", "items": None,
                                                 "detail": None, "attachments": None,
                                                 "diag": "未找到正文区——不入库"}):
                w = svc.get_scholarship_publications(detail_url=url)
            self.assertEqual(w["status"], "partial")
            self.assertEqual(w["items"][0]["text"], "旧正文")


class LocalTruncationTest(unittest.TestCase):
    def test_truncation_issue_when_emit_cursor_false(self):
        with temp_dir() as td:
            data = Path(td)
            seed_session(data, "acct-r2fix00000003")
            svc = JWWebService(SnapshotStore(data / "cache.sqlite3"), data)
            mapped = [{"entry_id": "e%d" % i, "name": "n%d" % i} for i in range(3)]
            w = svc._finalize_public_success(
                acct="acct-r2fix00000003", module=MODULE_DIGITAL_RESOURCES,
                source="src.lib", scope="s", safe_url="https://x",
                mapped=mapped, outcome="ok", diag="", total=3,
                issues=[], now=svc._now_fn(), limit=1,
                parser_version="t", evidence_ref="e", emit_cursor=False)
            self.assertEqual(len(w["items"]), 1)
            joined = json.dumps(w["issues"], ensure_ascii=False)
            self.assertIn("local_truncated", joined)
            self.assertIn("仅返回前 1 条", joined)
            self.assertIsNone(w["pagination"]["next_cursor"])


if __name__ == "__main__":
    unittest.main()
