# -*- coding: utf-8 -*-
"""Query a configured local we-mp-rss SQLite database without network access."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .mp_sources import guess_category, load_registry, registry_path, resolve_sources

_TZ = timezone(timedelta(hours=8))
_MAX_LIMIT = 50
_MAX_OFFSET = 1_000_000
_MILLISECONDS_THRESHOLD = 10_000_000_000
# Latest representable instant in the configured UTC+08:00 display timezone.
_MAX_DISPLAY_EPOCH = 253_402_271_999
_MAX_MP_IDS = 50
_MAX_KEYWORD_TERMS = 12
_MAX_KEYWORD_LEN = 50
_SHORT_HASH_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_SCOPES = ("all", "school", "school_confirmed")
_SCOPE_LABELS = {
    "all": "全部订阅（未做学校限定）",
    "school": "学校来源（ID 已确认 + 名称候选，ID 排除项剔除）",
    "school_confirmed": "仅登记表中按 ID 已确认的学校来源",
}
# Source databases can contain seconds and milliseconds. Invalid timestamps
# are excluded from bounded time searches and sort last in unbounded searches.
_TIMESTAMP_SECONDS_SQL = (
    "(CASE WHEN typeof(a.publish_time) IN ('integer','real') AND a.publish_time > 0 "
    f"THEN CASE WHEN a.publish_time >= {_MILLISECONDS_THRESHOLD} "
    "THEN a.publish_time / 1000.0 ELSE a.publish_time END END)"
)
_TIMESTAMP_SQL = ("(CASE WHEN " + _TIMESTAMP_SECONDS_SQL + f" BETWEEN 1 AND {_MAX_DISPLAY_EPOCH} "
                  "THEN CAST(" + _TIMESTAMP_SECONDS_SQL + " AS INTEGER) END)")

# 表结构自检结果缓存：键为 (路径, mtime_ns, size)，库文件变化即失效。
_SCHEMA_CACHE: dict[tuple[str, int, int], bool] = {}


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
    """Allow canonical HTTPS article links only; drop private/tracking parameters.

    目前只输出 22 位短链形态，作为保守筛选而非可用性验证。其他长度仅表示
    来源格式待核实，不能断言必然失效；包括已观察到的 75 位疑似占位链接。
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme.lower() != "https" or parts.hostname != "mp.weixin.qq.com"
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            return None
        if parts.path.startswith("/s/"):
            tail = parts.path[3:]
            if tail.endswith("/"):
                tail = tail[:-1]
            if not _SHORT_HASH_RE.fullmatch(tail):
                return None
            return "https://mp.weixin.qq.com/s/" + tail
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


def _classify_wechat_url(value) -> tuple[str | None, str]:
    """标明来源链接的形态与安全状态，不执行网络请求或回显被过滤值。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "missing"
    if not isinstance(value, str):
        # Do not decode a BLOB or other unexpected database value into a link.
        return None, "type_unsupported"
    url = _safe_wechat_url(value)
    if url is not None:
        return url, "format_accepted_unverified"
    if len(value) > 4096:
        return None, "safety_filtered"
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme.lower() == "https" and parts.hostname == "mp.weixin.qq.com"
                and parts.username is None and parts.password is None
                and parts.port in (None, 443)
                and (parts.path == "/s" or parts.path.startswith("/s/"))):
            return None, "format_unverified"
    except (TypeError, ValueError):
        pass
    return None, "safety_filtered"


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value)[:limit]


def _literal_like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _unavailable(reason: str, hint: str | None = None, **extra) -> dict:
    result = {"status": "unavailable", "module_id": "mp_articles",
              "query_time": _now_str(), "reason": reason}
    if hint:
        result["hint"] = hint
    result.update(extra)
    return result


def _normalize_ids(value) -> tuple[list[str] | None, str | None]:
    """把 mp_ids 规范成去重列表；None 表示未指定，空数组视为参数错误（不得扩成全订阅）。"""
    if value is None:
        return None, None
    if isinstance(value, str):
        raw = [part for part in value.split(",") if part.strip()]
        if not value.strip():
            return None, "参数无效：mp_ids 不能为空（不传表示不限制）"
    elif isinstance(value, (list, tuple)):
        raw = list(value)
        if not raw:
            return None, "参数无效：mp_ids 不能为空数组（不传表示不限制）"
    else:
        return None, "参数无效：mp_ids 应为公众号 ID 列表"
    if len(raw) > _MAX_MP_IDS:
        return None, "参数无效：mp_ids 最多 %d 个" % _MAX_MP_IDS
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, str):
            return None, "参数无效：mp_ids 每项应为文本"
        item = item.strip()
        if not item or len(item) > 255:
            return None, "参数无效：mp_ids 每项应为 1..255 字符的文本"
        if item not in seen:
            seen.add(item)
            out.append(item)
    if not out:
        return None, "参数无效：mp_ids 不能为空数组（不传表示不限制）"
    return out, None


def _normalize_terms(keyword, keywords) -> tuple[list[str] | None, str | None]:
    """把 keyword（单个）与 keywords（同义词组）合并为 OR 检索词，去重保序。"""
    terms: list[str] = []
    if keyword is not None:
        if not isinstance(keyword, str) or len(keyword) > 200:
            return None, "参数无效：keyword 应为不超过200字符的文本"
        if keyword.strip():  # 空串沿用旧行为：视为未指定
            terms.append(keyword.strip())
    if keywords is not None:
        if not isinstance(keywords, (list, tuple)):
            return None, "参数无效：keywords 应为文本列表"
        for item in keywords:
            if not isinstance(item, str):
                return None, "参数无效：keywords 每项应为文本"
            item = item.strip()
            if not item:  # 空项跳过，与 keyword 空串一致
                continue
            if len(item) > _MAX_KEYWORD_LEN:
                return None, "参数无效：keywords 每项应为 1..%d 字符的文本" % _MAX_KEYWORD_LEN
            terms.append(item)
    ordered = list(dict.fromkeys(terms))
    if len(ordered) > _MAX_KEYWORD_TERMS:
        return None, "参数无效：关键词合计最多 %d 个" % _MAX_KEYWORD_TERMS
    return ordered, None


class MpArticlesService:
    def __init__(self, db_path=None, sources_path=None):
        # Explicit callers and tests take precedence over the host environment.
        value = (db_path if db_path is not None else
                 os.environ.get("ZJGSU_MP_RSS_DB") or
                 Path(__file__).resolve().parents[3] / "we-mp-rss" / "data" / "db.db")
        self.db_path = Path(value) if value else None
        self.sources_path = Path(sources_path) if sources_path is not None else registry_path()

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

    def _schema_key(self):
        try:
            stat = self.db_path.stat()
            return (str(self.db_path), stat.st_mtime_ns, stat.st_size)
        except (AttributeError, OSError):
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

    def get_mp_articles(self, mp_name=None, keyword=None, days=7, limit=20, offset=0,
                        scope="all", mp_ids=None, keywords=None, count_total=True,
                        *, _include_internal_id=False):
        started = time.perf_counter()
        if not isinstance(_include_internal_id, bool):
            return _unavailable("参数无效：内部文章标识开关应为布尔值")
        for name, value, lower, upper in (("days", days, 0, 365),
                                          ("limit", limit, 1, None),
                                          ("offset", offset, 0, _MAX_OFFSET)):
            if (isinstance(value, bool) or not isinstance(value, int) or value < lower
                    or (upper is not None and value > upper)):
                return _unavailable("参数无效：%s 应为允许范围内的整数" % name)
        if mp_name is not None and (not isinstance(mp_name, str) or len(mp_name) > 200):
            return _unavailable("参数无效：mp_name 应为不超过200字符的文本")
        if not isinstance(scope, str) or scope.strip().lower() not in _SCOPES:
            return _unavailable("参数无效：scope 应为 all / school / school_confirmed")
        scope = scope.strip().lower()
        if not isinstance(count_total, bool):
            return _unavailable("参数无效：count_total 应为布尔值")
        wanted_ids, id_error = _normalize_ids(mp_ids)
        if id_error:
            return _unavailable(id_error)
        terms, term_error = _normalize_terms(keyword, keywords)
        if term_error:
            return _unavailable(term_error)
        limit = min(limit, _MAX_LIMIT)
        now = datetime.now(_TZ)

        where, params = ["a.status != 1000"], []
        if mp_name and mp_name.strip():
            where.append("f.mp_name LIKE ? ESCAPE '\\'")
            params.append(_literal_like(mp_name.strip()))
        if terms:
            clauses = []
            for term in terms:
                clauses.append("(a.title LIKE ? ESCAPE '\\' OR a.description LIKE ? ESCAPE '\\')")
                pattern = _literal_like(term)
                params.extend([pattern, pattern])
            where.append("(" + " OR ".join(clauses) + ")")
        if days:
            where.append(_TIMESTAMP_SQL + " >= ?")
            params.append(int((now - timedelta(days=days)).timestamp()))
            where.append(_TIMESTAMP_SQL + " <= ?")
            params.append(int(now.timestamp()))

        connect_started = time.perf_counter()
        try:
            if self.db_path is not None and self.db_path.is_dir():
                return _unavailable("采集库路径指向目录，需要数据库文件",
                                    "请检查 ZJGSU_MP_RSS_DB 指向的文件。")
        except OSError:
            pass  # retain the existing unavailable semantics for inaccessible paths
        conn = self._connect()
        connect_ms = (time.perf_counter() - connect_started) * 1000
        if conn is None:
            return _unavailable("采集库不存在或不可读",
                                "请将 ZJGSU_MP_RSS_DB 配置为本机 we-mp-rss SQLite 文件路径。")
        schema_ms = 0.0
        resolve_ms = 0.0
        query_ms = 0.0
        statements = 0
        scope_info = {"scope": scope, "scope_label": _SCOPE_LABELS[scope],
                      "requested_mp_ids": len(wanted_ids or []),
                      "scope_sources": {"confirmed": None, "pending": None,
                                        "excluded": None, "unclassified": None},
                      "selected_sources": {"matched": None, "confirmed": None, "pending": None},
                      "unmatched_mp_ids": None, "id_check": "not_checked",
                      "registry": None, "note": ""}
        try:
            # Both the count and rows must observe the same SQLite snapshot.
            conn.execute("BEGIN")
            key = self._schema_key()
            cached = _SCHEMA_CACHE.get(key) if key is not None else None
            if cached is True:
                ready = True
            else:
                check_started = time.perf_counter()
                ready = self._table_ready(conn)
                schema_ms = (time.perf_counter() - check_started) * 1000
                statements += 4
                if key is not None and ready:
                    if len(_SCHEMA_CACHE) > 8:
                        _SCHEMA_CACHE.clear()
                    _SCHEMA_CACHE[key] = True
            if not ready:
                return _unavailable("采集库表结构不匹配（articles/feeds 字段缺失）",
                                    "请核对 we-mp-rss 数据库版本与配置。")

            issues: list[dict] = []
            source_meta: dict[str, dict] = {}
            if scope != "all" or wanted_ids is not None:
                entries, meta = load_registry(self.sources_path)
                scope_info["registry"] = {"configured": meta["configured"],
                                          "error": meta["error"],
                                          "invalid_entries": meta["invalid_entries"]}
                if scope != "all" and meta["error"]:
                    # 排除规则无法保证时不静默降级：直接拒绝学校范围。
                    return _unavailable(
                        "来源登记表不可读或结构无效，学校范围无法保证排除规则",
                        "请修正 data/mp_sources.json（或 ZJGSU_MP_SOURCES 指向的文件）；"
                        "暂可用 scope=all 或 mp_ids 指定单号。",
                        scope_info=scope_info)
                if scope != "all" and meta["invalid_entries"]:
                    issues.append({"code": "registry_partial", "retryable": False,
                                   "safe_message": "登记表包含无效条目，无法保证来源排除规则。"})
                    return _unavailable(
                        "来源登记表存在无效条目，学校范围无法保证排除规则",
                        "请修正来源登记表后重试。", scope_info=scope_info, issues=issues)
                resolve_started = time.perf_counter()
                feeds = [(row[0], row[1]) for row in conn.execute(
                    "SELECT id, mp_name FROM feeds")]
                statements += 1
                if scope != "all":
                    if meta["configured"]:
                        feed_ids = {row[0] for row in feeds}
                        stale = {entry["id"] for entry in entries
                                 if entry["id"] and entry["id"] not in feed_ids}
                        scope_info["registry"]["not_in_local_feeds"] = len(stale)
                        if stale:
                            issues.append({"code": "registry_id_not_in_local_feeds", "retryable": False,
                                "safe_message": "登记表有 %d 个来源 ID 不在当前本地订阅表中；"
                                                "不代表这些公众号已注销。" % len(stale)})
                    resolution = resolve_sources(feeds, entries, scope)
                    allowed = resolution["allowed"]
                    scope_info["scope_sources"] = {
                        "confirmed": resolution["confirmed"],
                        "pending": resolution["pending"],
                        "excluded": resolution["excluded"],
                        "unclassified": resolution["unclassified"],
                    }
                    if resolution["conflicts"]:
                        issues.append({"code": "registry_conflict", "retryable": False,
                                       "safe_message": ("登记表内有 %d 处同 ID/同名冲突，"
                                                        "冲突项未授予确认或排除身份。"
                                                        % resolution["conflicts"])})
                        scope_info["registry"]["conflicts"] = resolution["conflicts"]
                        return _unavailable(
                            "来源登记表存在冲突，学校范围无法保证排除规则",
                            "请修正冲突的来源登记后重试。", scope_info=scope_info, issues=issues)
                    if not meta["configured"]:
                        issues.append({"code": "registry_missing", "retryable": False,
                                       "safe_message": ("未配置来源登记表：学校范围仅按名称候选，"
                                                        "排除规则不可用。")})
                else:
                    allowed = [{"mp_id": row[0], "mp_name": row[1], "status": "unscoped",
                                "category": guess_category(row[1])} for row in feeds]
                scope_info["id_check"] = "checked"
                known_ids = {record["mp_id"] for record in allowed}
                if wanted_ids is not None:
                    # mp_ids is already bounded to 50; do not silently hide misses.
                    scope_info["unmatched_mp_ids"] = [item for item in wanted_ids
                                                      if item not in known_ids]
                    allowed = [record for record in allowed
                               if record["mp_id"] in set(wanted_ids)]
                resolve_ms = (time.perf_counter() - resolve_started) * 1000
                if scope != "all":
                    scope_info["selected_sources"] = {
                        "matched": len(allowed),
                        "confirmed": sum(1 for r in allowed if r["status"] == "confirmed"),
                        "pending": sum(1 for r in allowed if r["status"] == "pending"),
                    }
                    scope_info["note"] = ("权威身份按 ID 判定；名称命中与 school_evidence 只算候选，"
                                          "未独立核实官方归属。")
                    if not allowed:
                        scope_info["performance"] = {
                            "total_ms": round((time.perf_counter() - started) * 1000, 3),
                            "connect_ms": round(connect_ms, 3),
                            "resolve_ms": round(resolve_ms, 3),
                            "sql_statements": statements, "mode": "local_scope_unresolved"}
                        return _unavailable(
                            "学校来源未解析：当前 scope=%s 下没有可查询来源" % scope,
                            "可在 data/mp_sources.json 按 ID 登记已确认来源（status=confirmed，"
                            "excluded 需带 id）；或显式用 scope=all、mp_ids 指定单号。",
                            scope_info=scope_info)
                else:
                    scope_info["selected_sources"]["matched"] = len(allowed)
                where.append("f.id IN (" + ",".join("?" * len(allowed)) + ")")
                params.extend(record["mp_id"] for record in allowed)
                source_meta = {record["mp_id"]: record for record in allowed}

            # Diagnose incomplete local source data in this same read snapshot.
            # The counts are for the complete local DB, not necessarily the
            # current school, keyword or date scope.
            diagnostic_started = time.perf_counter()
            diagnostic = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN f.id IS NULL AND a.status IS NOT NULL "
                "AND a.status != 1000 THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN a.status IS NULL THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN a.status IS NOT NULL AND a.status != 1000 "
                "AND " + _TIMESTAMP_SQL + " IS NULL THEN 1 ELSE 0 END),0) "
                "FROM articles a LEFT JOIN feeds f ON a.mp_id = f.id"
            ).fetchone()
            statements += 1
            source_gaps, status_gaps, time_gaps = diagnostic
            if source_gaps:
                issues.append({"code": "unmatched_article_source", "retryable": False,
                    "safe_message": "整个本地库有 %d 条状态已知的文章无法关联公众号；"
                                    "限定来源时无法判断其归属。" % source_gaps})
            if status_gaps:
                issues.append({"code": "unknown_article_status", "retryable": False,
                    "safe_message": "整个本地库有 %d 条文章状态未知，未将其当作有效文章。" % status_gaps})
            if days and time_gaps:
                issues.append({"code": "unknown_article_time", "retryable": False,
                    "safe_message": "整个本地库有 %d 条文章的时间无法判断，"
                                    "时间窗口内未计入；这些文章不一定属于当前筛选范围。" % time_gaps})
            # A full-subscription search can show non-deleted orphaned records
            # with unknown source. Scoped searches never guess a feed identity.
            join = "LEFT JOIN" if scope == "all" and wanted_ids is None and not mp_name else "JOIN"
            base = "FROM articles a " + join + " feeds f ON a.mp_id = f.id WHERE " + " AND ".join(where)
            select = ("SELECT a.id AS article_storage_id, f.id AS feed_id, f.mp_name, a.title, " + _TIMESTAMP_SQL + " AS publish_seconds, "
                      "a.url, a.description, a.mp_id ")
            order = " ORDER BY " + _TIMESTAMP_SQL + " DESC, a.id DESC"
            diagnostic_ms = (time.perf_counter() - diagnostic_started) * 1000
            query_started = time.perf_counter()
            if count_total:
                # Both the count and rows must observe the same SQLite snapshot.
                total = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
                statements += 1
                rows = conn.execute(select + base + order + " LIMIT ? OFFSET ?",
                                    [*params, limit, offset]).fetchall()
                statements += 1
                has_more = offset + len(rows) < total
            else:
                # 少一次 COUNT：多取一条判断是否还有下一页，总数语义为「未知」。
                rows = conn.execute(select + base + order + " LIMIT ? OFFSET ?",
                                    [*params, limit + 1, offset]).fetchall()
                statements += 1
                has_more = len(rows) > limit
                rows = rows[:limit]
                total = None
            query_ms = diagnostic_ms + (time.perf_counter() - query_started) * 1000
        except sqlite3.Error as exc:
            if "too many sql variables" in str(exc).lower():
                return _unavailable("当前查询超出 SQLite 参数上限",
                                    "请缩小公众号范围或减少关键词数量后重试。")
            return _unavailable("本机采集库查询失败", "请检查数据库是否完整、可读并与采集程序版本兼容。")
        finally:
            conn.close()

        post_started = time.perf_counter()
        articles = []
        omitted_urls = 0
        unsupported_url_types = 0
        for row in rows:
            url, url_status = _classify_wechat_url(row["url"])
            if url_status in ("safety_filtered", "format_unverified"):
                omitted_urls += 1
            elif url_status == "type_unsupported":
                unsupported_url_types += 1
            mp_id = _text(row["mp_id"], 255)
            record = source_meta.get(mp_id) or {}
            is_orphan = row["feed_id"] is None
            mp_name = _text(row["mp_name"], 256) or ("未知公众号" if is_orphan else "")
            title = _text(row["title"], 1000)
            publish_time = _fmt_ts(row["publish_seconds"])
            search_hint = (None if url is not None else {
                "title": title[:200], "mp_name": mp_name[:128],
                "note": "待搜索、待核对来源，可能无法找到原文；此线索不是原文链接。"})
            articles.append({"mp_name": mp_name,
                             "title": title,
                             "publish_time": publish_time,
                             "publish_time_known": bool(publish_time),
                             "url": url, "description": _text(row["description"], 120),
                             "url_status": url_status, "search_hint": search_hint,
                             "mp_id": mp_id,
                             "source_category": record.get("category")
                             or guess_category(row["mp_name"]),
                             "source_status": ("unknown" if is_orphan else
                                               record.get("status") or "unscoped")})
            if _include_internal_id:
                raw_id = row["article_storage_id"]
                articles[-1]["_article_storage_id"] = raw_id if isinstance(raw_id, str) else None
        has_more = bool(has_more)
        read_at = _now_str()
        flags = list(issues)
        if unsupported_url_types:
            flags.append({"code": "url_type_unsupported", "retryable": False,
                          "safe_message": "%d 条来源链接的数据库字段不是文本，已省略。"
                                          % unsupported_url_types})
        if omitted_urls:
            flags.append({"code": "unsafe_url_omitted", "retryable": False,
                          "safe_message": ("%d 条文章链接因安全规则或来源格式待核实而省略；"
                                           "22 位短链形态仅作筛选，不保证链接可用。" % omitted_urls)})
        if count_total is False:
            flags.append({"code": "total_unknown", "retryable": False,
                          "safe_message": "未统计匹配总数（count_total=false），只能按 has_more 续页。"})
        if scope != "all" and scope_info["selected_sources"]["pending"]:
            flags.append({"code": "school_scope_candidates", "retryable": False,
                          "safe_message": ("本次命中 %d 个名称/证据候选来源，官方归属未核实；"
                                           "结果不等于学校全部发布。"
                                           % scope_info["selected_sources"]["pending"])})
        gaps = ["仅覆盖本机数据库已有订阅，可能包含非校园公众号，学校来源与历史完整性未确认。",
                "结果为本次读取快照；不同页之间采集库可能变化，可能重复或遗漏。"]
        if count_total is False:
            gaps.append("本页未统计匹配总数（total_matched 未知），不能据此说共有多少条。")
        if scope != "all":
            gaps.append("来源限定按 ID 判定确认/排除，名称与 school_evidence 只用于候选；"
                        "未命中不代表学校没有发布。")
        coverage = {"total_matched": total, "total_matched_known": total is not None,
                    "returned": len(articles), "days_window": days or None,
                    "limit": limit, "complete": False}
        post_ms = (time.perf_counter() - post_started) * 1000
        total_ms = (time.perf_counter() - started) * 1000
        return {
            "status": ("partial" if source_gaps or status_gaps or (days and time_gaps) else "ok"),
            "module_id": "mp_articles", "query_time": read_at,
            "semantic": "本机采集库只读检索（不触网，不代表学校全部发布）",
            "data": {"articles": articles},
            "scope_info": scope_info,
            "coverage": coverage,
            "pagination": {"offset": offset, "has_more": has_more,
                           "next_offset": offset + len(articles) if has_more else None,
                           "stable_across_calls": False},
            "freshness": {"source": "local_db", "read_at": read_at,
                          "last_successful_collection_at": None,
                          "time_semantics": "来源时间戳可能为采集时间，真实发布时间及最近成功采集时间未知。"},
            "issues": flags,
            "gaps": gaps,
            "performance": {"local_db": {
                "mode": "live_local_db", "total_ms": round(total_ms, 3),
                "connect_ms": round(connect_ms, 3),
                "schema_check_ms": round(schema_ms, 3),
                "scope_resolve_ms": round(resolve_ms, 3),
                "query_ms": round(query_ms, 3),
                "postprocess_ms": round(post_ms, 3),
                "sql_statements": statements,
                "count_total": bool(count_total),
            }},
        }
