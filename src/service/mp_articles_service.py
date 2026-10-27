# -*- coding: utf-8 -*-
"""Query a configured local we-mp-rss SQLite database without network access."""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TZ = timezone(timedelta(hours=8))
_MAX_LIMIT = 50
_MAX_OFFSET = 1_000_000
_MILLISECONDS_THRESHOLD = 10_000_000_000
# Source databases can contain seconds and milliseconds. Invalid timestamps
# are excluded from bounded time searches and sort last in unbounded searches.
_TIMESTAMP_SQL = (
    "(CASE WHEN typeof(a.publish_time) IN ('integer','real') AND a.publish_time > 0 "
    f"THEN CAST(CASE WHEN a.publish_time >= {_MILLISECONDS_THRESHOLD} "
    "THEN a.publish_time / 1000.0 ELSE a.publish_time END AS INTEGER) END)"
)


def _now_str() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %z")


def _fmt_ts(seconds) -> str:
    try:
        seconds = int(seconds)
        if seconds <= 0:
            return ""
        return datetime.fromtimestamp(seconds, _TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _safe_wechat_url(value) -> str | None:
    """Allow canonical HTTPS article links only; drop private/tracking parameters."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme.lower() != "https" or parts.hostname != "mp.weixin.qq.com"
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            return None
        if re.fullmatch(r"/s/[A-Za-z0-9_-]+/?", parts.path):
            return urlunsplit(("https", "mp.weixin.qq.com", parts.path, "", ""))
        if parts.path != "/s":
            return None
        allowed = {"__biz", "mid", "idx", "sn"}
        entries = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                   if key in allowed]
        params = dict(entries)
        if len(entries) != 4 or set(params) != allowed:
            return None
        if (not re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", params["__biz"])
                or not re.fullmatch(r"[0-9]{1,20}", params["mid"])
                or not re.fullmatch(r"[0-9]{1,10}", params["idx"])
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", params["sn"])):
            return None
        query = urlencode([(key, params[key]) for key in ("__biz", "mid", "idx", "sn")])
        return urlunsplit(("https", "mp.weixin.qq.com", "/s", query, ""))
    except (TypeError, ValueError):
        return None


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value)[:limit]


def _literal_like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _unavailable(reason: str, hint: str | None = None) -> dict:
    result = {"status": "unavailable", "module_id": "mp_articles",
              "query_time": _now_str(), "reason": reason}
    if hint:
        result["hint"] = hint
    return result


class MpArticlesService:
    def __init__(self, db_path=None):
        # Explicit callers and tests take precedence over the host environment.
        value = (db_path if db_path is not None else
                 os.environ.get("ZJGSU_MP_RSS_DB") or
                 Path(__file__).resolve().parents[3] / "we-mp-rss" / "data" / "db.db")
        self.db_path = Path(value) if value else None

    def _connect(self):
        """Open the requested database via a correctly escaped read-only URI."""
        conn = None
        try:
            if self.db_path is None or not self.db_path.is_file():
                return None
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            conn.execute("PRAGMA query_only = ON")
            conn.row_factory = sqlite3.Row
            return conn
        except (OSError, ValueError, sqlite3.Error):
            if conn is not None:
                conn.close()
            return None

    @staticmethod
    def _table_ready(conn) -> bool:
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"articles", "feeds"} <= names:
                return False
            articles = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
            feeds = {r[1] for r in conn.execute("PRAGMA table_info(feeds)")}
            return ({"id", "mp_id", "title", "publish_time", "url", "description", "status"} <= articles
                    and {"id", "mp_name"} <= feeds)
        except sqlite3.Error:
            return False

    def get_mp_articles(self, mp_name=None, keyword=None, days=7, limit=20, offset=0):
        for name, value, lower, upper in (("days", days, 0, 365),
                                          ("limit", limit, 1, None),
                                          ("offset", offset, 0, _MAX_OFFSET)):
            if (isinstance(value, bool) or not isinstance(value, int) or value < lower
                    or (upper is not None and value > upper)):
                return _unavailable("参数无效：%s 应为允许范围内的整数" % name)
        for name, value in (("mp_name", mp_name), ("keyword", keyword)):
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                return _unavailable("参数无效：%s 应为不超过200字符的文本" % name)
        limit = min(limit, _MAX_LIMIT)
        now = datetime.now(_TZ)
        where, params = ["a.status != 1000"], []
        if mp_name and mp_name.strip():
            where.append("f.mp_name LIKE ? ESCAPE '\\'")
            params.append(_literal_like(mp_name.strip()))
        if keyword and keyword.strip():
            where.append("(a.title LIKE ? ESCAPE '\\' OR a.description LIKE ? ESCAPE '\\')")
            pattern = _literal_like(keyword.strip())
            params.extend([pattern, pattern])
        if days:
            where.append(_TIMESTAMP_SQL + " >= ?")
            params.append(int((now - timedelta(days=days)).timestamp()))
            where.append(_TIMESTAMP_SQL + " <= ?")
            params.append(int(now.timestamp()))

        conn = self._connect()
        if conn is None:
            return _unavailable("采集库不存在或不可读",
                                "请将 ZJGSU_MP_RSS_DB 配置为本机 we-mp-rss SQLite 文件路径。")
        try:
            # Both the count and rows must observe the same SQLite snapshot.
            conn.execute("BEGIN")
            if not self._table_ready(conn):
                return _unavailable("采集库表结构不匹配（articles/feeds 字段缺失）",
                                    "请核对 we-mp-rss 数据库版本与配置。")
            base = "FROM articles a JOIN feeds f ON a.mp_id = f.id WHERE " + " AND ".join(where)
            total = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
            rows = conn.execute(
                "SELECT f.mp_name, a.title, " + _TIMESTAMP_SQL + " AS publish_seconds, "
                "a.url, a.description, a.mp_id " + base +
                " ORDER BY " + _TIMESTAMP_SQL + " DESC, a.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        except sqlite3.Error:
            return _unavailable("本机采集库查询失败", "请检查数据库是否完整、可读并与采集程序版本兼容。")
        finally:
            conn.close()

        articles = []
        omitted_urls = 0
        for row in rows:
            url = _safe_wechat_url(row["url"])
            if row["url"] and url is None:
                omitted_urls += 1
            articles.append({"mp_name": _text(row["mp_name"], 256),
                             "title": _text(row["title"], 1000),
                             "publish_time": _fmt_ts(row["publish_seconds"]),
                             "url": url, "description": _text(row["description"], 120),
                             "mp_id": _text(row["mp_id"], 255)})
        has_more = offset + len(articles) < total
        read_at = _now_str()
        issues = []
        if omitted_urls:
            issues.append({"code": "unsafe_url_omitted", "retryable": False,
                           "safe_message": "%d 条文章链接不符合安全规则，已省略。" % omitted_urls})
        return {
            "status": "ok", "module_id": "mp_articles", "query_time": read_at,
            "data": {"articles": articles},
            "coverage": {"total_matched": total, "returned": len(articles),
                         "days_window": days or None, "limit": limit, "complete": False},
            "pagination": {"offset": offset, "has_more": has_more,
                           "next_offset": offset + len(articles) if has_more else None,
                           "stable_across_calls": False},
            "freshness": {"source": "local_db", "read_at": read_at,
                          "last_successful_collection_at": None,
                          "time_semantics": "来源时间戳可能为采集时间，真实发布时间及最近成功采集时间未知。"},
            "issues": issues,
            "gaps": ["仅覆盖本机数据库已有订阅，可能包含非校园公众号，学校来源与历史完整性未确认。",
                     "结果为本次读取快照；不同页之间采集库可能变化，可能重复或遗漏。"],
        }
