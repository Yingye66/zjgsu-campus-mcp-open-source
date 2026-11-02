"""Read a known local candidate through WeRead without persisting its text or credentials."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .mp_article_content_service import MpArticleContentService, _MAX_BYTES

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_UPSTREAM_ROOT = _PROJECT_ROOT.parent / 'we-mp-rss'
_UPSTREAM_PYTHON = _UPSTREAM_ROOT / '.venv' / 'Scripts' / 'python.exe'
_CREDENTIAL_FILE = _UPSTREAM_ROOT / 'data' / 'wx.lic'
_ENDPOINT = 'https://weread.qq.com/web/mp/content'
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'
# The upstream environment owns PyYAML. Its child returns only the one account's
# cookie to this process via a private pipe. No shell, file creation, or logging.
_AUTH_SCRIPT = r'''
import json, pathlib, sys
try:
 import yaml
 p=pathlib.Path(sys.argv[1])
 if p.stat().st_size > 262144: raise ValueError()
 doc=yaml.safe_load(p.read_text(encoding="utf-8")) or {}
 if not isinstance(doc,dict): raise ValueError()
 data=doc.get("weread_data") or {}
 if isinstance(data,str): data=json.loads(data)
 if not isinstance(data,dict): raise ValueError()
 accounts=data.get("accounts")
 if not isinstance(accounts,list) or len(accounts)!=1 or not isinstance(accounts[0],dict):
  print(json.dumps({"account_count":len(accounts) if isinstance(accounts,list) else 0}))
 else:
  cookie=accounts[0].get("cookie")
  print(json.dumps({"account_count":1,"cookie":cookie if isinstance(cookie,str) else ""}))
except Exception:
 print("{}")
'''


def read_weread_auth():
    try:
        result = subprocess.run(
            [str(_UPSTREAM_PYTHON), '-I', '-B', '-c', _AUTH_SCRIPT, str(_CREDENTIAL_FILE)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=8, check=False, shell=False,
        )
        if result.returncode != 0 or len(result.stdout) > 131072:
            return None
        doc = json.loads(result.stdout)
        if not isinstance(doc, dict) or doc.get('account_count') != 1:
            return None
        cookie = doc.get('cookie')
        if not isinstance(cookie, str) or not cookie or len(cookie) > 65536 or '\r' in cookie or '\n' in cookie:
            return None
        return cookie
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(review_id, cookie):
    url = _ENDPOINT + '?' + urllib.parse.urlencode({'reviewId': review_id})
    req = urllib.request.Request(url, headers={
        'Cookie': cookie, 'User-Agent': _UA, 'Referer': 'https://weread.qq.com/',
        'Accept': 'text/html,application/json', 'Accept-Encoding': 'identity',
    })
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=15) as response:
            encoding = (response.headers.get('Content-Encoding') or '').lower().strip()
            if encoding not in ('', 'identity'):
                return (response.status, None, None)
            return (response.status, response.read(_MAX_BYTES + 1), response.headers.get_content_charset())
    except urllib.error.HTTPError as exc:
        return (exc.code, None, None)


def _review_id(article_id, mp_id):
    # The upstream normalizes both IDs by removing MP_WXS_ and stores
    # <feed>-<review>. Anything outside this reversible structure is refused.
    if not isinstance(mp_id, str) or not re.fullmatch(r'MP_WXS_[A-Za-z0-9]{1,100}', mp_id):
        return None
    if not isinstance(article_id, str) or len(article_id) > 500:
        return None
    normalized_feed = mp_id[len('MP_WXS_'):]
    prefix = normalized_feed + '-' + normalized_feed + '_'
    if not article_id.startswith(prefix):
        return None
    remainder = article_id[len(prefix):]
    if not re.fullmatch(r'[A-Za-z0-9_~.-]{1,300}', remainder) or 'MP_WXS_' in remainder:
        return None
    review = mp_id + '_' + remainder
    if (mp_id + '-' + review).replace('MP_WXS_', '') != article_id:
        return None
    return review


def _norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()) if isinstance(value, str) else ''


def _failure(code, reason):
    return {'status': 'unavailable', 'module_id': 'mp_weread_content', 'code': code, 'reason': reason}


class _RateLimiter:
    """Same-process requests share one lock; wait at most 2 seconds for it."""
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self._lock = threading.Lock()
        self._last = None
        self._clock = clock
        self._sleep = sleep

    def run(self, fetch, review, cookie):
        if not self._lock.acquire(timeout=2):
            return False, None
        try:
            if self._last is not None:
                delay = max(0.0, 2.0 - (self._clock() - self._last))
                if delay:
                    self._sleep(delay)
            self._last = self._clock()
            return True, fetch(review, cookie)
        finally:
            self._lock.release()


_RATE_LIMITER = _RateLimiter()


class MpWereadContentService:
    def __init__(self, auth_loader=None, fetch=None, limiter=None):
        self._auth_loader = auth_loader or read_weread_auth
        self._fetch = fetch or _fetch
        self._limiter = limiter or _RATE_LIMITER

    def read(self, article_id, mp_id, expected_title=None, expected_source=None, max_chars=5000):
        review = _review_id(article_id, mp_id)
        if not review:
            return _failure('invalid_article_identity', '本地文章标识无法安全还原。')
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 500 <= max_chars <= 50000:
            return _failure('invalid_argument', '正文长度参数无效。')
        try:
            cookie = self._auth_loader()
        except Exception:
            cookie = None
        if not isinstance(cookie, str) or not cookie or len(cookie) > 65536 or '\r' in cookie or '\n' in cookie:
            return _failure('auth_required', '微信读书账号凭据不可用或账号数不唯一。')
        try:
            acquired, response = self._limiter.run(self._fetch, review, cookie)
            if not acquired:
                return _failure('rate_limited', '微信读书请求正在执行，请稍后再试。')
            status, raw, charset = response
        except Exception:
            return _failure('network_error', '微信读书正文请求失败。')
        if isinstance(status, int) and 300 <= status < 400:
            return _failure('redirect_refused', '微信读书正文请求发生重定向，已停止。')
        if status in (429, 499):
            return _failure('rate_limited', '微信读书请求受到限制，请稍后再试。')
        if status != 200:
            return _failure('http_error', '微信读书正文服务不可用。')
        if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_BYTES:
            return _failure('response_invalid', '微信读书正文响应为空、过大或格式异常。')
        if raw.lstrip().startswith((b'{', b'[')):
            try:
                obj = json.loads(raw)
                code = obj.get('errCode', obj.get('errcode')) if isinstance(obj, dict) else None
            except (ValueError, TypeError):
                code = None
            if code in (-2010, -2012, -2014, -2041):
                return _failure('auth_or_quota', '微信读书登录态或访问额度不可用。')
            return _failure('response_invalid', '微信读书未返回正文页面。')
        # Delegate parser, safe charset handling and interception checks to the
        # existing single-article reader; the synthetic URL is never returned.
        try:
            result = MpArticleContentService(fetch=lambda _: (raw, charset)).read(
                'https://mp.weixin.qq.com/s/' + 'A' * 22, max_chars=max_chars)
        except Exception:
            return _failure('response_invalid', '微信读书正文页面无法解析。')
        if result.get('status') != 'ok':
            return _failure('content_unavailable', '微信读书页面没有可用正文或需要验证。')
        data = result.get('data', {})
        pairs = [(_norm(expected_title), _norm(data.get('title'))),
                 (_norm(expected_source), _norm(data.get('mp_name')))]
        if any(expected and actual and len(expected) < limit and len(actual) < limit and expected != actual
               for (expected, actual), limit in zip(pairs, (200, 100))):
            return _failure('identity_mismatch', '正文页面与本地文章标题或公众号不一致。')
        verified = all(expected and actual and len(expected) < limit and len(actual) < limit and expected == actual
                       for (expected, actual), limit in zip(pairs, (200, 100)))
        coverage = dict(result.get('coverage') or {})
        if not verified:
            coverage['complete'] = False
        return {'status': 'ok', 'module_id': 'mp_weread_content', 'source': 'weread',
                'identity_status': 'metadata_consistent' if verified else 'unverified',
                'data': data, 'coverage': coverage}
