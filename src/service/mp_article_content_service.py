# -*- coding: utf-8 -*-
"""按需读取单篇公众号正文（不落盘、不缓存，读完即弃）。

设计边界：
- 只接受已通过白名单校验的微信文章链接（22 位短链或规范化的 /s?__biz&mid&idx&sn）；
- 只取公开页面，不带任何 Cookie/登录态，不写本机数据库，也不改动采集库；
- 页面拿不到正文（验证页、已删除、结构变化）时如实返回 unavailable，不编造内容；
- 返回正文最多 `max_chars`（默认 20000），超出部分标记 truncated，避免把整页塞给模型。
"""
from __future__ import annotations

import html
import gzip
import http.client
import io
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from .mp_articles_service import _safe_wechat_url

_TZ = timezone(timedelta(hours=8))
_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
_MAX_BYTES = 6 * 1024 * 1024
_DEFAULT_MAX_CHARS = 20000
_BLOCK_TEXT = ("环境异常", "参数错误", "该内容已被发布者删除", "此内容因违规无法查看",
               "请在微信客户端打开链接", "访问过于频繁", "该文章已被删除",
               "该内容因用户投诉无法查看")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
              "meta", "param", "source", "track", "wbr"}
_MEDIA_TAGS = {"img", "video", "audio", "svg", "canvas", "iframe", "table"}


def _now_str() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %z")


class _BodyExtractor(HTMLParser):
    """提取唯一且结构闭合的 #js_content，不将失衡的页面当正文。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.in_content = False
        self.content_count = 0
        self.closed = False
        self.malformed = False
        self.has_media = False
        self.parts: list[str] = []
        self.visible: list[str] = []
        self.title = ""
        self.mp_name = ""
        self._og_title = ""
        self._title_stack: list[str] = []
        self._name_stack: list[str] = []
        self._title_parts: list[str] = []
        self._name_parts: list[str] = []
        self._metadata_malformed = False
        self._skip = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if self._skip:
            return
        if tag in ("script", "style"):
            self._skip = tag
            return
        if self._title_stack:
            if tag not in _VOID_TAGS:
                self._title_stack.append(tag)
        elif tag == "h1" and "rich_media_title" in classes:
            self._title_stack = [tag]
            self._title_parts = []
        if self._name_stack:
            if tag not in _VOID_TAGS:
                self._name_stack.append(tag)
        elif attrs.get("id") == "js_name":
            if tag in _VOID_TAGS:
                self._metadata_malformed = True
            else:
                self._name_stack = [tag]
                self._name_parts = []
        if attrs.get("id") == "js_content":
            self.content_count += 1
            if self.in_content or self.content_count > 1 or tag in _VOID_TAGS:
                self.malformed = True
                return
            self.in_content = True
            self.stack = [tag]
            return
        if self.in_content:
            if tag not in _VOID_TAGS:
                self.stack.append(tag)
            if tag in _MEDIA_TAGS:
                self.has_media = True
            if tag in ("p", "br", "div", "section", "li", "tr", "h1", "h2", "h3", "h4"):
                self.parts.append("\n")
        if tag == "meta" and attrs.get("property") == "og:title" and attrs.get("content"):
            if not self._og_title:
                self._og_title = attrs["content"]
                self.title = self._og_title

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip:
                self._skip = None
            return
        if self.in_content:
            if tag not in _VOID_TAGS:
                if not self.stack or self.stack[-1] != tag:
                    self.malformed = True
                else:
                    self.stack.pop()
                    if not self.stack:
                        self.in_content = False
                        self.closed = True
        if tag not in _VOID_TAGS:
            if self._title_stack:
                if self._title_stack[-1] != tag:
                    self._metadata_malformed = True
                    self._title_stack.clear()
                    self._title_parts.clear()
                else:
                    self._title_stack.pop()
                    if not self._title_stack and not self._og_title and not self.title:
                        self.title = "".join(self._title_parts)
            if self._name_stack:
                if self._name_stack[-1] != tag:
                    self._metadata_malformed = True
                    self._name_stack.clear()
                    self._name_parts.clear()
                else:
                    self._name_stack.pop()
                    if not self._name_stack and not self.mp_name:
                        self.mp_name = "".join(self._name_parts)

    def handle_data(self, data):
        if self._skip:
            return
        self.visible.append(data)
        if self.in_content:
            self.parts.append(data)
        if self._title_stack:
            self._title_parts.append(data)
        if self._name_stack:
            self._name_parts.append(data)

    @property
    def metadata_incomplete(self) -> bool:
        return self._metadata_malformed or bool(self._title_stack or self._name_stack)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)


def _clean(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _FetchProblem(Exception):
    """内容读取受控失败，不携带 URL、响应体或系统异常信息。"""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        safe_target = _safe_wechat_url(target)
        if not safe_target:
            raise _FetchProblem("重定向目标不符合微信文章链接白名单")
        return super().redirect_request(req, fp, code, msg, headers, safe_target)


def _decode_page(raw: bytes, declared_charset: str | None) -> str:
    """严格解码声明的字符集，避免乱码被误作成功正文。"""
    if not isinstance(raw, bytes):
        raise _FetchProblem("页面响应格式无效")
    if len(raw) > _MAX_BYTES:
        raise _FetchProblem("页面超出大小限制")
    if raw.startswith(b"\xef\xbb\xbf"):
        charset = "utf-8-sig"
    else:
        charset = declared_charset
        if not charset:
            match = re.search(rb'<meta\b[^>]*?charset\s*=\s*["\']?\s*([a-zA-Z0-9._-]+)',
                              raw[:8192], re.I)
            charset = match.group(1).decode("ascii") if match else "utf-8"
    charset = charset.strip().lower().replace("_", "-")
    aliases = {"utf8": "utf-8", "gb2312": "gb18030", "gbk": "gb18030"}
    charset = aliases.get(charset, charset)
    if charset not in {"utf-8", "utf-8-sig", "gb18030", "big5"}:
        raise _FetchProblem("页面字符集不受支持")
    try:
        return raw.decode(charset, "strict")
    except UnicodeDecodeError as exc:
        raise _FetchProblem("页面字符解码失败") from exc


def _is_blocked(visible: str, body: str) -> bool:
    """识别提示正文或验证动作，避免把文章中引用的提示语直接当成拦截页。"""
    visible = re.sub(r"\s+", "", visible)
    body = re.sub(r"\s+", "", body)
    body = re.sub(r"^(?:温馨提示|页面提示|微信提示|提示)[:：,，]?", "", body, count=1)
    verification = ("完成验证", "安全验证", "人机验证", "请验证", "验证后", "访问验证")
    if not body:
        return (any(token in visible for token in _BLOCK_TEXT[2:])
                or (("环境异常" in visible or "参数错误" in visible)
                    and any(token in visible for token in verification)))
    for token in _BLOCK_TEXT:
        if body == token:
            return True
        if token in _BLOCK_TEXT[2:] and body.startswith(token):
            remainder = body[len(token):].lstrip("，。,:：;；!！ ")
            if not remainder or remainder.startswith(("请", "无法", "点击", "返回", "重新", "查看", "微信")):
                return True
    if (len(body) <= 120 and body.startswith(("请", "需要", "完成"))
            and any(token in body for token in verification)
            and any(token in body for token in ("访问", "继续", "打开", "阅读"))):
        return True
    if "环境异常" in visible or "参数错误" in visible:
        if body.startswith(("环境异常", "参数错误")) and any(token in body for token in verification):
            return True
        if body.startswith(("请完成验证", "完成验证", "请验证", "安全验证", "去验证")):
            return True
    return False


class MpArticleContentService:
    def __init__(self, fetch=None):
        # fetch(url) -> bytes；测试注入合成页面，生产用 urllib。
        self._fetch = fetch or self._http_fetch

    @staticmethod
    def _http_fetch(url: str):
        request = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
        })
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(request, timeout=15) as response:
            raw = response.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise _FetchProblem("页面超出大小限制")
            encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
            if encoding == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                        raw = gz.read(_MAX_BYTES + 1)
                except (OSError, EOFError, ValueError, zlib.error) as exc:
                    raise _FetchProblem("页面压缩数据无效") from exc
                if len(raw) > _MAX_BYTES:
                    raise _FetchProblem("解压后的页面超出大小限制")
            elif encoding not in ("", "identity"):
                raise _FetchProblem("页面压缩格式不受支持")
            charset = response.headers.get_content_charset()
            return raw, charset

    def read(self, url, max_chars: int = _DEFAULT_MAX_CHARS) -> dict:
        safe_url = _safe_wechat_url(url)
        if not safe_url:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(),
                    "reason": "链接不合法：只接受 https://mp.weixin.qq.com 的 22 位短链或规范化的 __biz/mid/idx/sn 形式"}
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not (500 <= max_chars <= 50000):
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "reason": "参数无效：max_chars 应为 500..50000 的整数"}
        try:
            raw = self._fetch(safe_url)
        except _FetchProblem as exc:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url, "reason": "抓取失败：%s" % exc}
        except urllib.error.HTTPError as exc:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url,
                    "reason": "抓取失败：HTTP %s" % exc.code}
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url,
                    "reason": "抓取失败：%s" % type(exc).__name__}
        charset = None
        if isinstance(raw, tuple) and len(raw) == 2:
            raw, charset = raw
        if not raw:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url, "reason": "抓取失败：页面为空"}
        try:
            page = _decode_page(raw, charset)
        except _FetchProblem as exc:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url, "reason": "抓取失败：%s" % exc}
        parser = _BodyExtractor()
        try:
            parser.feed(page)
            parser.close()
        except Exception:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url, "reason": "页面结构无法解析"}
        candidate_body = _clean("".join(parser.parts))
        if (_is_blocked(_clean(" ".join(parser.visible)), candidate_body)
                or candidate_body in ("参数错误", "环境异常")):
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url,
                    "reason": "微信返回拦截页/提示页（环境异常、已删除或需在客户端打开），未取到正文"}
        if parser.malformed or parser.in_content or (parser.content_count and not parser.closed):
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url,
                    "reason": "正文区块结构不完整或存在多个区块，不能可靠提取正文"}
        body = candidate_body
        if not body:
            return {"status": "unavailable", "module_id": "mp_article_content",
                    "query_time": _now_str(), "url": safe_url,
                    "reason": ("正文区块没有可提取的文字，可能是图片/视频类文章" if parser.content_count
                               else "页面里没有正文区块（js_content），页面结构可能已变化")}
        truncated = len(body) > max_chars
        text = body[:max_chars]
        issues = ([] if not truncated else [{"code": "text_truncated", "retryable": False,
                                           "safe_message": "正文超过 %d 字，已截断。" % max_chars}])
        if parser.metadata_incomplete:
            issues.append({"code": "metadata_incomplete", "retryable": False,
                           "safe_message": "页面标题或公众号名称的结构不完整，未返回不可靠的部分。"})
        return {
            "status": "ok", "module_id": "mp_article_content", "query_time": _now_str(),
            "url": safe_url,
            "data": {"title": _clean(parser.title)[:200],
                     "mp_name": _clean(parser.mp_name)[:100],
                     "text": text, "char_count": len(body)},
            "coverage": {"truncated": truncated, "max_chars": max_chars,
                         "complete": not truncated and not parser.has_media and not parser.metadata_incomplete},
            "freshness": {"source": "live_page", "read_at": _now_str(),
                          "time_semantics": "本次实时抓取公开页面；文章发布时间未从页面解析，需以原文页显示为准。"},
            "issues": issues,
            "gaps": ["只读公开页面，不带登录态：付费/仅粉丝可见/已删除的文章取不到。",
                     "正文为页面文本抽取，图片、表格、视频与格式会丢失。"],
        }
