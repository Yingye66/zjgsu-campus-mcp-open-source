# -*- coding: utf-8 -*-
"""单篇公众号正文读取的合成测试（不触网）：注入假页面，校验抽取、拦截页、参数与截断。"""
from __future__ import annotations

import json
import gzip
import http.client
import io
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import patch

from src.service.mp_article_content_service import MpArticleContentService

HASH = "A" * 22
URL = "https://mp.weixin.qq.com/s/" + HASH

PAGE = """<html><head><meta property="og:title" content="测试文章标题"></head><body>
<h1 class="rich_media_title">测试文章标题</h1>
<a id="js_name">浙商大测试号</a>
<div id="js_content">
  <p>第一段内容，含 <span>行内标签</span>。</p>
  <p>&nbsp;&nbsp;第二段：奖学金 5000 元 &amp; 名额 30 人。</p>
  <script>var x = 1;</script>
</div>
</body></html>"""

BLOCK_PAGE = "<html><body><div>环境异常</div><p>完成验证后即可继续访问</p></body></html>"
NO_BODY_PAGE = "<html><body><h1>只有标题</h1></body></html>"


class ContentTests(unittest.TestCase):
    def svc(self, payload=b"", error=None):
        def fetch(url):
            if error is not None:
                raise error
            return payload if isinstance(payload, bytes) else payload.encode("utf-8")
        return MpArticleContentService(fetch=fetch)

    def test_extracts_text_and_metadata(self):
        r = self.svc(PAGE).read(URL)
        self.assertEqual(r["status"], "ok")
        text = r["data"]["text"]
        self.assertIn("第一段内容", text)
        self.assertIn("行内标签", text)
        self.assertIn("奖学金 5000 元 & 名额 30 人", text)
        self.assertNotIn("var x", text)          # script 不混入正文
        self.assertEqual(r["data"]["title"], "测试文章标题")
        self.assertEqual(r["data"]["mp_name"], "浙商大测试号")
        self.assertFalse(r["coverage"]["truncated"])
        self.assertEqual(r["freshness"]["source"], "live_page")

    def test_nested_metadata_preserves_all_title_and_account_text(self):
        page = ('<html><body><h1 class="rich_media_title">校园<span>活动</span>通知</h1>'
                '<a id="js_name">浙商大<span>教务</span>处</a>'
                '<div id="js_content"><p>完整正文</p></div></body></html>')
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["title"], "校园活动通知")
        self.assertEqual(result["data"]["mp_name"], "浙商大教务处")

    def test_og_title_overrides_h1_even_when_meta_appears_later(self):
        page = ('<html><body><h1 class="rich_media_title">页面标题</h1>'
                '<meta property="og:title" content="正式标题">'
                '<div id="js_content">完整正文</div></body></html>')
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["title"], "正式标题")

    def test_unclosed_name_metadata_is_not_returned_as_verified_name(self):
        page = ('<html><body><a id="js_name">原名<span>后缀</span>'
                '<div id="js_content">完整正文</div>')
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["mp_name"], "")
        self.assertIn("metadata_incomplete", [issue["code"] for issue in result["issues"]])

    def test_unclosed_title_metadata_is_not_returned_as_verified_title(self):
        page = ('<html><body><h1 class="rich_media_title">标题<span>后缀</span>'
                '<div id="js_content">完整正文</div>')
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["title"], "")
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("metadata_incomplete", [issue["code"] for issue in result["issues"]])

    def test_empty_http_response_reports_empty_not_missing_content(self):
        def https_open(handler, request):
            headers = Message()
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 200)
            response.msg = "OK"
            return response
        with patch.object(urllib.request.HTTPSHandler, "https_open", https_open):
            result = MpArticleContentService().read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("页面为空", result["reason"])
        self.assertNotIn("data", result)

    def test_block_page_is_unavailable_not_fabricated(self):
        r = self.svc(BLOCK_PAGE).read(URL)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("拦截页", r["reason"])
        self.assertNotIn("data", r)

    def test_page_without_content_block_is_unavailable(self):
        r = self.svc(NO_BODY_PAGE).read(URL)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("没有正文区块", r["reason"])

    def test_long_text_is_truncated_with_flag(self):
        big = PAGE.replace("第一段内容", "长" * 1200)
        r = self.svc(big).read(URL, max_chars=500)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["coverage"]["truncated"])
        self.assertLessEqual(len(r["data"]["text"]), 500)
        self.assertEqual({i["code"] for i in r["issues"]}, {"text_truncated"})

    def test_rejects_links_that_are_not_canonical(self):
        for bad in ("http://mp.weixin.qq.com/s/" + HASH,           # 非 https
                    "https://mp.weixin.qq.com/s/" + "W" * 75,       # 75 位占位哈希
                    "https://evil.example.com/s/" + HASH,
                    "https://mp.weixin.qq.com/s?__biz=x&mid=1",     # 参数不全
                    "", None):
            with self.subTest(bad=bad):
                r = self.svc(PAGE).read(bad)
                self.assertEqual(r["status"], "unavailable")
                self.assertIn("链接不合法", r["reason"])

    def test_max_chars_bounds(self):
        for value in (499, 50001, True, "1000"):
            with self.subTest(value=value):
                self.assertEqual(self.svc(PAGE).read(URL, max_chars=value)["status"],
                                 "unavailable")

    def test_network_errors_are_reported_without_traceback_leak(self):
        for exc in (urllib.error.HTTPError(URL, 404, "Not Found", None, None),
                    urllib.error.URLError("boom"),
                    TimeoutError("slow")):
            with self.subTest(exc=type(exc).__name__):
                r = self.svc(error=exc).read(URL)
                self.assertEqual(r["status"], "unavailable")
                self.assertTrue(r["reason"].startswith("抓取失败"))
                self.assertNotIn("Traceback", json.dumps(r, ensure_ascii=False))

    def test_empty_response_is_unavailable(self):
        r = self.svc(b"").read(URL)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("页面为空", r["reason"])

    def test_json_serializable_and_no_local_path(self):
        r = self.svc(PAGE).read(URL)
        text = json.dumps(r, ensure_ascii=False)
        self.assertNotIn("D:\\\\", text)
        self.assertNotIn("/home/", text)

    def test_block_or_deleted_page_with_content_container_is_unavailable(self):
        for notice in ("环境异常，请完成验证后再访问", "该内容已被发布者删除", "参数错误"):
            with self.subTest(notice=notice):
                page = '<html><body><div id="js_content">' + notice + '</div></body></html>'
                result = self.svc(page).read(URL)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("data", result)

    def test_real_article_may_discuss_the_words_environment_error(self):
        page = '<html><body><div id="js_content"><p>环境异常是软件故障的一种表现。</p></div></body></html>'
        self.assertEqual(self.svc(page).read(URL)["status"], "ok")

    def test_article_quoting_block_messages_is_not_itself_a_block_page(self):
        for sentence in ("科普：平台提示“该内容已被发布者删除”，这是常见错误。建议检查文章状态。",
                         "排查环境异常时，完成验证后再试，可以恢复正常访问。"):
            with self.subTest(sentence=sentence):
                page = '<html><body><div id="js_content"><p>' + sentence + '</p></div></body></html>'
                result = self.svc(page).read(URL)
                self.assertEqual(result["status"], "ok")
                self.assertIn(sentence, result["data"]["text"])

    def test_notice_outside_body_with_verification_action_is_not_article(self):
        page = '<html><body><h1>环境异常</h1><div id="js_content">请完成验证\n去验证</div></body></html>'
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)

    def test_notice_prefix_does_not_hide_deleted_page(self):
        page = '<div id="js_content">温馨提示：该内容已被发布者删除</div>'
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)

    def test_short_verification_and_deleted_notices_without_environment_marker(self):
        for notice in ("请完成验证后即可继续访问", "请在微信中完成安全验证后继续访问",
                       "该文章已被删除", "该内容因用户投诉无法查看"):
            with self.subTest(notice=notice):
                page = '<div id="js_content">' + notice + '</div>'
                result = self.svc(page).read(URL)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("data", result)

    def test_long_article_discussing_new_notice_variants_is_not_rejected(self):
        text = ("本篇是关于平台提示的说明。" * 12
                + "例如，页面可能显示“该文章已被删除”或“该内容因用户投诉无法查看”。"
                + "用户也可能看到“请完成验证后即可继续访问”，应当核查来源。")
        result = self.svc('<div id="js_content">' + text + '</div>').read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertIn("该文章已被删除", result["data"]["text"])

    def test_exact_500_and_501_character_truncation_boundary(self):
        for length, expected_truncated in ((500, False), (501, True)):
            with self.subTest(length=length):
                page = '<div id="js_content">' + "字" * length + '</div>'
                result = self.svc(page).read(URL, max_chars=500)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["data"]["char_count"], length)
                self.assertEqual(len(result["data"]["text"]), 500)
                self.assertEqual(result["coverage"]["truncated"], expected_truncated)

    def test_incomplete_http_response_is_controlled_and_never_leaks_body(self):
        error = http.client.IncompleteRead(b"synthetic-secret", 100)
        result = self.svc(error=error).read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)
        self.assertNotIn("synthetic-secret", json.dumps(result, ensure_ascii=False))

    def test_unclosed_content_does_not_claim_footer_as_complete_body(self):
        page = '<div id="js_content"><p>正文一<div>正文二<p>正文三</div><div>页脚-点赞-分享</div>'
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)

    def test_multiple_content_containers_do_not_silently_merge(self):
        page = '<div id="js_content">正文一</div><div id="js_content">另一份正文</div>'
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)

    def test_meta_declared_gbk_decodes_without_replacement_characters(self):
        page = '<html><head><meta charset="gbk"></head><body><div id="js_content">中文正文</div></body></html>'
        result = self.svc(page.encode("gbk")).read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["text"], "中文正文")

    def test_bad_encoding_is_unavailable_not_a_garbled_success(self):
        raw = b'<div id="js_content">\xff\xfe\xfa</div>'
        result = self.svc(raw).read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)

    def test_control_characters_removed_and_image_only_reported(self):
        r = self.svc('<div id="js_content">前\x00后\x1f</div>').read(URL)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["data"]["text"], "前后")
        picture = self.svc('<div id="js_content"><img src="x"></div>').read(URL)
        self.assertEqual(picture["status"], "unavailable")
        self.assertIn("图片", picture["reason"])

    def test_cross_host_redirect_is_rejected_before_second_request(self):
        seen = []
        def https_open(handler, request):
            seen.append(request.full_url)
            headers = Message()
            headers["Location"] = "http://127.0.0.1/private"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response
        def http_open(handler, request):
            seen.append(request.full_url)
            raise AssertionError("redirect must not be requested")
        with patch.object(urllib.request.HTTPSHandler, "https_open", https_open), \
             patch.object(urllib.request.HTTPHandler, "http_open", http_open):
            result = MpArticleContentService().read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(seen, [URL])

    def test_forced_gzip_is_decoded_with_bounded_read(self):
        def https_open(handler, request):
            headers = Message()
            headers["Content-Encoding"] = "gzip"
            headers["Content-Type"] = "text/html; charset=utf-8"
            response = urllib.response.addinfourl(io.BytesIO(gzip.compress(PAGE.encode())), headers,
                                                  request.full_url, 200)
            response.msg = "OK"
            return response
        with patch.object(urllib.request.HTTPSHandler, "https_open", https_open):
            result = MpArticleContentService().read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertIn("第一段内容", result["data"]["text"])

    def test_same_host_article_redirect_is_canonicalized_and_has_no_cookie(self):
        other = "https://mp.weixin.qq.com/s/" + "B" * 22
        seen = []
        def https_open(handler, request):
            seen.append((request.full_url, request.get_header("Cookie")))
            headers = Message()
            if len(seen) == 1:
                headers["Location"] = other + "?token=synthetic-secret"
                response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
            else:
                response = urllib.response.addinfourl(io.BytesIO(PAGE.encode()), headers, request.full_url, 200)
                response.msg = "OK"
            return response
        with patch.object(urllib.request.HTTPSHandler, "https_open", https_open):
            result = MpArticleContentService().read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen, [(URL, None), (other, None)])
        self.assertNotIn("synthetic-secret", json.dumps(result, ensure_ascii=False))

    def test_gzip_decompression_is_bounded_and_invalid_stream_rejected(self):
        def compressed_response(payload):
            def https_open(handler, request):
                headers = Message()
                headers["Content-Encoding"] = "gzip"
                response = urllib.response.addinfourl(io.BytesIO(payload), headers, request.full_url, 200)
                response.msg = "OK"
                return response
            return https_open
        for payload in (gzip.compress(b"A" * 1000), b"not-gzip"):
            with self.subTest(payload_type="gzip" if payload.startswith(b"\x1f") else "invalid"):
                with patch("src.service.mp_article_content_service._MAX_BYTES", 100), \
                     patch.object(urllib.request.HTTPSHandler, "https_open", compressed_response(payload)):
                    result = MpArticleContentService().read(URL)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("data", result)

    def test_text_with_images_does_not_claim_complete_content(self):
        page = '<div id="js_content"><p>文字正文</p><img src="image.png"></div>'
        result = self.svc(page).read(URL)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["coverage"]["complete"])
        self.assertFalse(result["coverage"]["truncated"])

    def test_redirect_chain_has_bounded_number_of_requests(self):
        seen = []
        def https_open(handler, request):
            seen.append(request.full_url)
            headers = Message()
            headers["Location"] = "https://mp.weixin.qq.com/s/" + chr(ord("B") + len(seen)) * 22
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response
        with patch.object(urllib.request.HTTPSHandler, "https_open", https_open):
            result = MpArticleContentService().read(URL)
        self.assertEqual(result["status"], "unavailable")
        self.assertLessEqual(len(seen), 4)


if __name__ == "__main__":
    unittest.main()
