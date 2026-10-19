# -*- coding: utf-8 -*-
"""B03 r3 复验反馈修复回归（2026-10-16 下午第三批；全部合成，不触网）：

- ① Cookie expires=0 语义（不 or None 变会话 Cookie）；
- ② xszkdh 相对 Location 不再 NameError；:invalid 端口受控（mypage/flow）；
- ③ 公示列表限定 news_list 区域 + 来源域校验（外域仿公示不混入）；
- ④ 正文区 class token 精确匹配 + div 深度闭合（闭合后 nav/附件不混入）；
- ⑤ 附件 fragment 候选（a.pdf#page=2）识别并净化去 fragment。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from src.adapters.campus.mypage import (_hop_check, _restore_session_cookies,
                                         _xsc_publication_entries,
                                         fetch_digital_resources,
                                         fetch_scholarship_publications)

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
                           "path": path, "secure": bool(secure), "expires": expires})

    def __iter__(self):
        return iter(type("C", (), {"name": i["name"], "domain": i["domain"]})()
                    for i in list(self.items))


class _FakeSession:
    def __init__(self, routes=None):
        self.cookies = _FakeJar()
        self.headers = {}
        self._routes = routes or []
        self.calls = []

    def get(self, url, timeout=20, allow_redirects=False, params=None, **kw):
        self.calls.append(("GET", url, allow_redirects))
        for m, prefix, resp in self._routes:
            if m == "GET" and url.startswith(prefix):
                return resp
        return _FakeResp(404, "")


class ExpiresZeroTest(unittest.TestCase):
    def test_expires_zero_kept_as_zero(self):
        sess = _FakeSession()
        _restore_session_cookies(sess, [{"name": "E0", "value": "V",
                                         "domain": "uia.zjgsu.edu.cn", "path": "/",
                                         "secure": False, "expires": 0}])
        self.assertEqual(len(sess.cookies.items), 1)
        self.assertEqual(sess.cookies.items[0]["expires"], 0)  # 不是 None

    def test_expires_none_is_session_cookie(self):
        sess = _FakeSession()
        _restore_session_cookies(sess, [{"name": "EN", "value": "V",
                                         "domain": "uia.zjgsu.edu.cn", "path": "/",
                                         "secure": False, "expires": None}])
        self.assertIsNone(sess.cookies.items[0]["expires"])


class AuthBranchControlledTest(unittest.TestCase):
    def test_xszkdh_relative_location_no_nameerror(self):
        import requests as _requests
        routes = [
            ("GET", "https://uia.zjgsu.edu.cn/cas/login",
             _FakeResp(302, "", {"Location": "https://xszkdh.zjgsu.edu.cn/sso/login?type=65"})),
            ("GET", "https://xszkdh.zjgsu.edu.cn/sso/login",
             _FakeResp(200, "document.cookie = 'zbpToken=T; path=/';")),
            ("GET", "https://xszkdh.zjgsu.edu.cn/relative",
             _FakeResp(302, "", {"Location": "/sso/next?type=65"})),
        ]
        routes[1] = ("GET", "https://xszkdh.zjgsu.edu.cn/sso/login",
                     _FakeResp(302, "", {"Location": "/sso/next?type=65"}))
        with mock.patch.object(_requests, "Session", return_value=_FakeSession(routes)):
            from src.adapters.campus.mypage import fetch_xszkdh_token
            res = fetch_xszkdh_token(FAKE_COOKIES)  # 相对跳转路径——不得 NameError
        self.assertIn(res["outcome"], ("upstream_error", "network_error", "http_error"))

    def test_hop_check_invalid_port_controlled(self):
        _h, err = _hop_check("https://uia.zjgsu.edu.cn:invalid/cas/login",
                             ("uia.zjgsu.edu.cn",))
        self.assertIn("非法", err)

    def test_flow_invalid_port_controlled(self):
        import requests as _requests
        sess = _FakeSession([
            ("GET", "https://flow.zjgsu.edu.cn/login/Login.jsp",
             _FakeResp(302, "", {"Location": "https://flow.zjgsu.edu.cn:bad/x"})),
        ])
        with mock.patch.object(_requests, "Session", return_value=sess):
            from src.adapters.campus.flow import fetch_my_requests
            res = fetch_my_requests(FAKE_COOKIES, view_scope="mine")
        self.assertEqual(res["outcome"], "upstream_error")
        self.assertIn("非法", res["diag"])


class ListRegionTest(unittest.TestCase):
    def test_outbound_impostor_excluded(self):
        html = ("<html><body>"
                "<div class='nav'><a href='https://evil.example.com/2026/1015/c1827a999/page.htm' "
                "title='外域仿公示标题外域仿公示标题'>外域仿公示标题外域仿公示标题</a></div>"
                "<div class='col_news_list listcon'><ul class='news_list list2'>"
                "<li><a href='/2026/1015/c1827a232808/page.htm' title='真实公示标题'>真实公示标题</a>"
                "<span class='news_meta'>2026-10-15</span></li>"
                "</ul></div></body></html>")
        items = _xsc_publication_entries(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://xsc.zjgsu.edu.cn/2026/1015/c1827a232808/page.htm")

    def test_relative_resolved_and_domain_checked(self):
        html = ("<ul class='news_list'>"
                "<li><a href='/2026/1012/c1827a232695/page.htm'>公示条目标题</a>"
                "<span class='news_meta'>2026-10-12</span></li></ul>")
        items = _xsc_publication_entries(html)
        self.assertEqual(items[0]["url"],
                         "https://xsc.zjgsu.edu.cn/2026/1012/c1827a232695/page.htm")


class BodyElementBoundaryTest(unittest.TestCase):
    def _detail(self, html):
        import requests as _requests
        routes = [("GET", "https://xsc.zjgsu.edu.cn/2026/1012/c1827a232695/page.htm",
                   _FakeResp(200, html))]
        with mock.patch.object(_requests, "Session", return_value=_FakeSession(routes)):
            return fetch_scholarship_publications(
                detail_url="https://xsc.zjgsu.edu.cn/2026/1012/c1827a232695/page.htm")

    def test_substring_class_not_matched(self):
        html = ("<html><body>"
                "<div class='not_wp_article_content_suffix'>suffix 误配正文正文正文正文</div>"
                "</body></html>")
        res = self._detail(html)
        self.assertEqual(res["outcome"], "parse_error")  # token 精确匹配不误配

    def test_closed_element_excludes_after_nav(self):
        html = ("<html><body>"
                "<div class='wp_articlecontent'><p>公示正文内容公示正文内容公示正文内容公示正文内容公示正文内容公示正文内容</p>"
                "<a href='/_upload/article/files/aa/bb/inner.pdf'>内部附件</a></div>"
                "<div class='nav-footer'><a href='/_upload/article/files/cc/dd/outside.pdf'>闭合后附件</a>"
                "闭合后的导航文本闭合后的导航文本</div>"
                "</body></html>")
        res = self._detail(html)
        self.assertEqual(res["outcome"], "ok")
        self.assertIn("公示正文内容", res["detail"])
        self.assertNotIn("闭合后的导航", res["detail"])
        urls = [a["url"] for a in res["attachments"]]
        self.assertTrue(any("inner.pdf" in u for u in urls))
        self.assertFalse(any("outside.pdf" in u for u in urls))

    def test_fragment_attachment_recognized_and_stripped(self):
        html = ("<html><body>"
                "<div class='wp_articlecontent'>正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文"
                "<a href='/_upload/article/files/aa/bb/list.pdf#page=2'>名单（第2页）</a>"
                "</div></body></html>")
        res = self._detail(html)
        self.assertEqual(res["outcome"], "ok")
        urls = [a["url"] for a in res["attachments"]]
        self.assertTrue(any("list.pdf" in u for u in urls))
        self.assertFalse(any("#page=2" in u for u in urls))  # fragment 已去


class DigitalRegressionGuard(unittest.TestCase):
    def test_normal_path_still_ok(self):
        import requests as _requests
        html = ('<div class="list-content"><ul class="list">'
                '<li><a href="https://kns.cnki.net/" title="知网网络版">知网网络版</a>'
                '<a href="/2019/0115/c1020a37873/page.htm" class="pull-right">【资源简介】</a>'
                '</li></ul></div>')
        routes = [("GET", "http://lib.zjgsu.edu.cn/1020/list.htm", _FakeResp(200, html))]
        with mock.patch.object(_requests, "Session", return_value=_FakeSession(routes)):
            res = fetch_digital_resources(category="chinese_journal")
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["items"][0]["name"], "知网网络版")


if __name__ == "__main__":
    unittest.main()


class R4NoRegionFallbackTest(unittest.TestCase):
    """r4 反馈：无 news_list 时不得整页回退——导航内同源历史链接不得成为"当前公示"。"""

    def test_no_news_list_navigation_not_scanned(self):
        # 同源导航含 c1827a 历史链接 + 空 wp_article_list——无列表区
        html = ("<html><body>"
                "<div class='wp_article_list'></div>"
                "<div class='nav'>"
                "<a href='/2026/0929/c1827a232186/page.htm' title='历史公示标题'>历史公示标题</a>"
                "</div></body></html>")
        import src.adapters.campus.mypage as mp
        self.assertIsNone(mp._xsc_publication_entries(html))  # 模板缺失 → None

        import requests as _requests
        routes = [("GET", "https://xsc.zjgsu.edu.cn/1827/list.htm", _FakeResp(200, html))]
        with mock.patch.object(_requests, "Session", return_value=_FakeSession(routes)):
            res = mp.fetch_scholarship_publications(page=1)
        self.assertEqual(res["outcome"], "parse_error")
        self.assertIn("列表区", res["diag"])
        self.assertIsNone(res["items"])  # 不返回导航历史链接

    def test_normal_list_unaffected(self):
        html = ("<ul class='news_list'>"
                "<li><a href='/2026/1015/c1827a232808/page.htm'>公示标题样本</a>"
                "<span class='news_meta'>2026-10-15</span></li></ul>")
        import src.adapters.campus.mypage as mp
        items = mp._xsc_publication_entries(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "公示标题样本")
