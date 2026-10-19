# -*- coding: utf-8 -*-
"""应用服务：编排「读取 → 分类 → 缓存 → 包装」，产出只读工具的结果。

覆盖两个业务模块（共用同一套包装/缓存/游标规则）：
- campus.timetable（账号域：本机会话；账号/日期段隔离；逐端点成功码白名单）
- campus.events（公共域 public：公开来源匿名读取，不需要登录会话）

关键语义（实现依据：docs/技术设计与Agent交接.md 第 7–8/2.6/2.9 节、信息覆盖清单第 7 节）：
- 账号域从本机认证上下文取得（timetable）；公共来源固定 public 域（events），工具不接收账号参数；
- 成功（含合法空结果）→ 写快照 + last_success_at；失败只更新 last_attempt_at/last_issue，
  旧快照与 last_success_at 不变；有旧快照的失败 → partial；否则 unavailable；
- 新鲜度属于「该键对应的真实快照」；is_stale 未配置规则时为 null；
- 事件状态（进行中/未开始/已结束/已取消）在响应组装时按当前时间计算，缓存中只存原始字段；
- cursor 绑定（账号域 + 模块 + 规范化范围 + 快照 id + 偏移），不匹配 → snapshot_expired，
  cursor 路径不触发上游请求。
"""
from __future__ import annotations

import base64
import json
import re
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.campus.timetable import BASE as CAMPUS_BASE
from ..adapters.campus.timetable import COURSE_PATH, fetch_timetable
from ..adapters.campus.transport import UrllibTransport
from ..adapters.career.events import (BASE as JYW_BASE, EVENT_TYPE_XJH, LIST_PATH as JYW_LIST_PATH,
                                      SOURCE_JYW_XJH, derive_status, fetch_events_range)
from ..auth.session import get_active, load_session
from ..domain import errors as E
from ..domain.models import (MODULE_TEACHER_PAGES, ACCOUNT_PUBLIC, MODULE_EVENTS, MODULE_TIMETABLE,
                             SOURCE_COURSE_API, MODULE_AWARDS_PUBLICATIONS,
                             MODULE_DIGITAL_RESOURCES)
from .capabilities import KNOWN_MODULE_IDS, build_modules

TZ = timezone(timedelta(hours=8))  # 中国无夏令时：固定 +08:00（免 tzdata 依赖）
LIMIT_MAX = 500
EVENTS_MAX_DAYS = 14  # 单次事件查询的天数上限（逐日查询，控制请求量；超出报 invalid_arguments）
# R17（交接 2.11）：严格 yyyy-MM-dd（strptime 会放行 "2026-1-1" 等非补零形式）
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PARSER_VERSION_TIMETABLE = "campus-timetable-parser/0.1.0"
PARSER_VERSION_EVENTS = "campus-events-parser/0.2.0"  # 0.2.0：单时刻不补结束；旧缓存按 time_range 重解析
EVIDENCE_REF_TIMETABLE = ("docs/运行与验收.md 验收 #1（单周读取）；"
                          "docs/代码链报告-2026-10-13.md 第 4 节（静态链）")
EVIDENCE_REF_EVENTS = "validation/career/取证记录.md（2026-10-13 公开源实测）"


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _parse_strict_dates(start_date, end_date):
    """R17：严格校验两个日期均为 yyyy-MM-dd（先正则后 strptime）。
    返回 (d1, d2)；不合法返回 None（调用方报 invalid_arguments，且不触网）。"""
    if not (isinstance(start_date, str) and isinstance(end_date, str)):
        return None
    if not (_RE_DATE.match(start_date) and _RE_DATE.match(end_date)):
        return None
    try:
        d1 = datetime.strptime(start_date, "%Y-%m-%d").date()
        d2 = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return d1, d2


def _age_seconds(now: datetime, iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        t = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return max(0, int((now - t).total_seconds()))


def _encode_cursor(acct: str, module: str, scope: str, sid: str, off: int) -> str:
    payload = {"v": 1, "acct": acct, "module": module, "scope": scope, "sid": sid, "off": off}
    return base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _decode_cursor(cursor: str) -> dict | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("v") == 1:
            return payload
    except Exception:
        pass
    return None


class CampusService:
    def __init__(self, store, data_dir: Path | str, transport=None, now_fn=None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.transport = transport if transport is not None else UrllibTransport()
        self._now_fn = now_fn if now_fn is not None else (lambda: datetime.now(TZ))

    # ---------- 内部工具 ----------
    def _ctx(self):
        """返回 (acct, session)；二者可能为 None。"""
        acct = get_active(self.data_dir)
        session = load_session(self.data_dir, acct) if acct else None
        return acct, session

    def _health_row(self, acct: str, module: str, source: str) -> dict | None:
        rows = self.store.get_health_rows(acct, module)
        for r in rows:
            if r["source_id"] == source:
                return r
        return None

    def _freshness(self, health: dict | None, snap: dict | None, now: datetime,
                   source: str) -> dict:
        ls = snap["fetched_at"] if snap else None
        la = health["last_attempt_at"] if health else None
        age = _age_seconds(now, ls)
        return {
            "last_attempt_at": la,
            "last_success_at": ls,
            "data_age_seconds": age,
            "is_stale": None,  # 未配置新鲜度规则（规则应由实际需求配置，不猜来源要求）
            "by_source": [{
                "source_id": source,
                "last_attempt_at": la,
                "last_success_at": ls,
                "data_age_seconds": age,
                "is_stale": None,
            }],
        }

    @staticmethod
    def _snap_block(scope: str, snap: dict | None, source: str) -> dict:
        return {
            "source_id": source,
            "query_scope": scope,
            "snapshot_id": snap["snapshot_id"] if snap else None,
            "complete": bool(snap["complete"]) if snap else False,
            "consistency": snap["consistency"] if snap else "unknown",
            "missing_ranges": list(snap["missing_ranges"]) if snap else [],
        }

    @staticmethod
    def _source_refs(snap: dict | None, source: str, safe_url: str,
                     parser_version: str, evidence_ref: str) -> list[dict]:
        return [{
            "source_id": source,
            "safe_entry_url": safe_url,  # 仅脱敏地址，不含票据/参数
            "fetched_at": snap["fetched_at"] if snap else None,
            "parser_version": parser_version,
            "evidence_ref": evidence_ref,
        }]

    def _page(self, acct: str, module: str, scope: str, sid: str, items: list,
              offset: int, limit: int):
        offset = max(0, min(offset, len(items)))
        page = items[offset:offset + limit]
        has_more = offset + len(page) < len(items)
        next_cursor = _encode_cursor(acct, module, scope, sid,
                                     offset + len(page)) if has_more else None
        return page, has_more, next_cursor

    @staticmethod
    def _empty_wrapper(status: str, module: str, issues: list[dict],
                       snap_block: dict, freshness: dict, source_refs: list[dict]) -> dict:
        return {
            "status": status,
            "module_id": module,
            "items": [],
            "coverage": {"source_snapshots": [snap_block]},
            "pagination": {"has_more": False, "next_cursor": None, "result_snapshot_id": None},
            "freshness": freshness,
            "source_refs": source_refs,
            "issues": issues,
        }

    def _unavailable(self, module: str, source: str, scope: str, issue: dict,
                     safe_url: str, parser_version: str, evidence_ref: str) -> dict:
        # R54：全部公共来源（events/teachers/notices/job_fairs/career/services）失败域统一
        _PUBLIC_MODULES = {MODULE_EVENTS, MODULE_TEACHER_PAGES, "notices.public",
                           "campus.job_fairs", "career.info", "services.directory",
                           MODULE_AWARDS_PUBLICATIONS, MODULE_DIGITAL_RESOURCES}
        acct_for_health = ACCOUNT_PUBLIC if module in _PUBLIC_MODULES else (
            self._ctx()[0] or "")
        health = self._health_row(acct_for_health, module, source) if acct_for_health else None
        now = self._now_fn()
        # §2.64：公共来源失败响应带真实健康时间（snap=None 时通用 _freshness 会
        # 丢掉 health 里的旧成功时间——此处对公共来源手工构造）
        if module in _PUBLIC_MODULES and health:
            old_ls = health.get("last_success_at")
            freshness = {
                "last_attempt_at": now.isoformat(timespec="seconds"),
                "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(now, old_ls),
                "is_stale": None,
                "by_source": [{"source_id": source,
                               "last_attempt_at": now.isoformat(timespec="seconds"),
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(now, old_ls),
                               "is_stale": None}],
            }
        else:
            freshness = self._freshness(health, None, now, source)
        return self._empty_wrapper(
            "unavailable", module, [issue],
            self._snap_block(scope, None, source),
            freshness,
            self._source_refs(None, source, safe_url, parser_version, evidence_ref))

    # ---------- 工具 1：能力清单 ----------
    def list_capabilities(self) -> dict:
        now = self._now_fn()
        return {
            "status": "ok",
            "generated_at": now.isoformat(timespec="seconds"),
            "coverage_note": ("覆盖率为待遍历状态（coverage_unknown）：本人可见菜单尚未全量遍历；"
                              "台账各行分别标注证据等级（静态映射 / 真实读取）。"
                              "已实现工具不代表 MCP 客户端真实调用已验收（accepted 另行记录）。"),
            "modules": build_modules(),
        }

    # ================= 工具 2：课表（账号域） =================
    def get_timetable(self, start_date: str, end_date: str,
                      cursor: str | None = None, limit: int = 50) -> dict:
        module, source = MODULE_TIMETABLE, SOURCE_COURSE_API
        scope = "%s~%s" % (start_date, end_date)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= LIMIT_MAX):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..%d 的整数" % LIMIT_MAX),
                                     CAMPUS_BASE + COURSE_PATH, PARSER_VERSION_TIMETABLE,
                                     EVIDENCE_REF_TIMETABLE)
        parsed = _parse_strict_dates(start_date, end_date)
        if parsed is None:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "日期须为 yyyy-MM-dd 格式"),
                                     CAMPUS_BASE + COURSE_PATH, PARSER_VERSION_TIMETABLE,
                                     EVIDENCE_REF_TIMETABLE)
        d1, d2 = parsed
        if d2 < d1:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "end_date 须不早于 start_date"),
                                     CAMPUS_BASE + COURSE_PATH, PARSER_VERSION_TIMETABLE,
                                     EVIDENCE_REF_TIMETABLE)

        acct, session = self._ctx()
        if not acct or not session:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     CAMPUS_BASE + COURSE_PATH, PARSER_VERSION_TIMETABLE,
                                     EVIDENCE_REF_TIMETABLE)
        now = self._now_fn()

        if cursor:
            return self._serve_cursor(acct, module, source, scope, cursor, limit, now,
                                      CAMPUS_BASE + COURSE_PATH, PARSER_VERSION_TIMETABLE,
                                      EVIDENCE_REF_TIMETABLE, "timetable")

        if str(session.get("user_type")) != "2":
            return self._unavailable(module, source, scope,
                                     E.issue(E.UNSUPPORTED, False,
                                             "当前会话身份非学生（user_type=%s），课表模块不适用"
                                             % session.get("user_type")),
                                     CAMPUS_BASE + COURSE_PATH, PARSER_VERSION_TIMETABLE,
                                     EVIDENCE_REF_TIMETABLE)

        fetch = fetch_timetable(self.transport, session.get("zjgsu_token", ""),
                                start_date, end_date)
        done_at = self._now_fn()  # R16：新鲜度/记录时间按取数完成时刻
        now_s = done_at.isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)

        if fetch.outcome in ("ok", "ok_empty"):
            entries = fetch.entries or []
            snap = self.store.upsert_snapshot_and_health(
                acct, module, source, scope,
                items=entries, fetched_at=now_s, complete=False, consistency="best_effort",
                missing_ranges=[], empty_evidence=(fetch.outcome == "ok_empty"),
                success=True, at=now_s)
            page, has_more, next_cursor = self._page(acct, module, scope,
                                                     snap["snapshot_id"], entries, 0, limit)
            return {
                "status": "ok",
                "module_id": module,
                "items": page,
                "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
                "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                               "result_snapshot_id": snap["snapshot_id"]},
                "freshness": self._freshness(self._health_row(acct, module, source), snap,
                                             done_at, source),
                "source_refs": self._source_refs(snap, source, CAMPUS_BASE + COURSE_PATH,
                                                 PARSER_VERSION_TIMETABLE, EVIDENCE_REF_TIMETABLE),
                "issues": [self._completeness_issue_timetable(len(entries))],
            }

        code = E.OUTCOME_TO_CODE.get(fetch.outcome, E.PARSER_MISMATCH)
        retryable = fetch.outcome in ("network_error", "http_error")
        # F29-R6：App 课表端点的业务码 99 结构化标记——仅当
        # 来源=src.course-api、原始业务码=99、且本机持有 App 会话（token 非空）时设置。
        # classify 的 outcome 语义不变（99 仍不映射 auth_required）；其他来源/其他码不标记。
        auth_hint = None
        if (fetch.outcome == "business_error"
                and getattr(fetch, "raw_business_code", None) == "99"
                and source == SOURCE_COURSE_API
                and session.get("zjgsu_token")):
            auth_hint = "app_session"
        isu = E.issue(code, retryable, fetch.diag, auth_hint=auth_hint)
        self.store.record_issue(acct, module, source, now_s, scope, isu)
        old = self.store.get_snapshot(acct, module, source, scope)
        if old:
            return self._build_partial(acct, module, source, scope, old, isu, limit,
                                       done_at,
                                       CAMPUS_BASE + COURSE_PATH, PARSER_VERSION_TIMETABLE,
                                       EVIDENCE_REF_TIMETABLE, "timetable")
        return self._empty_wrapper(
            "unavailable", module, [isu],
            self._snap_block(scope, None, source),
            self._freshness(self._health_row(acct, module, source), None, done_at, source),
            self._source_refs(None, source, CAMPUS_BASE + COURSE_PATH,
                              PARSER_VERSION_TIMETABLE, EVIDENCE_REF_TIMETABLE))

    # ================= 工具 3：宣讲会（公共域） =================
    def get_campus_events(self, start_date: str, end_date: str,
                          event_type: str = EVENT_TYPE_XJH,
                          keyword: str | None = None,
                          cursor: str | None = None, limit: int = 50) -> dict:
        module, source = MODULE_EVENTS, SOURCE_JYW_XJH
        acct = ACCOUNT_PUBLIC
        kw = (keyword or "").strip() or None
        scope = "%s~%s" % (start_date, end_date) + (("|kw=%s" % kw) if kw else "")
        safe_url = JYW_BASE + JYW_LIST_PATH

        if event_type != EVENT_TYPE_XJH:
            return self._unavailable(module, source, scope,
                                     E.issue(E.UNSUPPORTED, False,
                                             "event_type 目前仅支持「%s」（招聘会/招聘信息等"
                                             "其他类型尚未接入）" % EVENT_TYPE_XJH),
                                     safe_url, PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= LIMIT_MAX):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..%d 的整数" % LIMIT_MAX),
                                     safe_url, PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS)
        parsed = _parse_strict_dates(start_date, end_date)
        if parsed is None:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "日期须为 yyyy-MM-dd 格式"),
                                     safe_url, PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS)
        d1, d2 = parsed
        if d2 < d1:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "end_date 须不早于 start_date"),
                                     safe_url, PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS)
        if (d2 - d1).days + 1 > EVENTS_MAX_DAYS:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "单次查询最多 %d 天（来源按日查询；请分段查询）"
                                             % EVENTS_MAX_DAYS),
                                     safe_url, PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS)

        now = self._now_fn()
        if cursor:
            return self._serve_cursor(acct, module, source, scope, cursor, limit, now,
                                      safe_url, PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS,
                                      "events")

        fetch = fetch_events_range(self.transport, start_date, end_date, keyword=kw)
        done_at = self._now_fn()  # R16：区分尝试开始与成功取数完成时刻
        now_s = done_at.isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)

        if fetch.failed_day is None and fetch.outcome in ("ok", "ok_empty"):
            entries = fetch.entries or []
            snap = self.store.upsert_snapshot_and_health(
                acct, module, source, scope,
                items=entries, fetched_at=now_s,
                complete=bool(fetch.complete), consistency="best_effort",
                missing_ranges=list(fetch.missing_ranges),
                empty_evidence=(fetch.outcome == "ok_empty" and fetch.complete),
                success=True, at=now_s)
            out_items = self._events_with_status(entries, done_at)
            page, has_more, next_cursor = self._page(acct, module, scope,
                                                     snap["snapshot_id"], out_items, 0, limit)
            issue_c = self._completeness_issue_for(module, snap)
            # 契约（交接 2.11）：限页截断（complete=false）→ partial（与第 8 节一致）
            status = "ok" if snap["complete"] else "partial"
            return {
                "status": status,
                "module_id": module,
                "items": page,
                "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
                "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                               "result_snapshot_id": snap["snapshot_id"]},
                "freshness": self._freshness(self._health_row(acct, module, source), snap,
                                             done_at, source),
                "source_refs": self._source_refs(snap, source, safe_url,
                                                 PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS),
                "issues": [i for i in [issue_c] if i],
            }

        # 读取失败：具名 issue + 旧快照/last_success_at 保留
        fail_outcome = fetch.failure_outcome or fetch.outcome
        code = E.OUTCOME_TO_CODE.get(fail_outcome, E.PARSER_MISMATCH)
        retryable = fail_outcome in ("network_error", "http_error")
        isu = E.issue(code, retryable, fetch.failure_diag or fetch.diag)
        old = self.store.get_snapshot(acct, module, source, scope)
        if fetch.entries:
            # 多日部分失败：保留已读部分（partial），失败日记入缺口；不丢弃已取数据
            # 若既有快照更完整（complete=true）则不覆盖，直接在其上回退
            if old and old.get("complete"):
                self.store.record_issue(acct, module, source, now_s, scope, isu)
                base_snap = old
            else:
                base_snap = self.store.upsert_snapshot_and_health(
                    acct, module, source, scope,
                    items=fetch.entries, fetched_at=now_s, complete=False,
                    consistency="best_effort",
                    missing_ranges=list(fetch.missing_ranges), empty_evidence=False,
                    success=False, at=now_s, issue=isu)
            return self._build_partial(acct, module, source, scope, base_snap, isu, limit,
                                       done_at, safe_url, PARSER_VERSION_EVENTS,
                                       EVIDENCE_REF_EVENTS, "events")
        self.store.record_issue(acct, module, source, now_s, scope, isu)
        if old:
            return self._build_partial(acct, module, source, scope, old, isu, limit,
                                       done_at, safe_url, PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS,
                                       "events")
        return self._empty_wrapper(
            "unavailable", module, [isu],
            self._snap_block(scope, None, source),
            self._freshness(self._health_row(acct, module, source), None, done_at, source),
            self._source_refs(None, source, safe_url,
                              PARSER_VERSION_EVENTS, EVIDENCE_REF_EVENTS))

    # ---------- 共用：partial / cursor ----------
    def _build_partial(self, acct: str, module: str, source: str, scope: str,
                       old: dict, isu: dict, limit: int, now: datetime,
                       safe_url: str, parser_version: str, evidence_ref: str,
                       kind: str) -> dict:
        items = old["items"]
        if kind == "events":
            items = self._events_with_status(items, now)
        page, has_more, next_cursor = self._page(acct, module, scope, old["snapshot_id"],
                                                 items, 0, limit)
        issue_c = self._completeness_issue_for(module, old)
        return {
            "status": "partial",
            "module_id": module,
            "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": old["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source), old, now,
                                         source),
            "source_refs": self._source_refs(old, source, safe_url,
                                             parser_version, evidence_ref),
            "issues": [isu] + ([issue_c] if issue_c else []),
        }

    def _serve_cursor(self, acct: str, module: str, source: str, scope: str,
                      cursor: str, limit: int, now: datetime,
                      safe_url: str, parser_version: str, evidence_ref: str,
                      kind: str) -> dict:
        payload = _decode_cursor(cursor)
        # R66：scope 传 ""/None 时用 payload 里的首屏 scope
        #（目录/预选首屏 scope 含 keyword/page——消费调用方无法还原）
        if not scope and payload:
            scope = payload.get("scope") or ""
        expired = E.issue(E.SNAPSHOT_EXPIRED, False,
                          "游标无效或与当前账号/查询不匹配，或对应快照已更新——"
                          "请重新发起查询（不带 cursor）")
        if (not payload or payload.get("acct") != acct
                or payload.get("module") != module or payload.get("scope") != scope):
            return self._empty_wrapper(
                "unavailable", module, [expired],
                self._snap_block(scope, None, source),
                self._freshness(self._health_row(acct, module, source), None, now, source),
                self._source_refs(None, source, safe_url, parser_version, evidence_ref))
        snap = self.store.get_snapshot(acct, module, source, scope)
        if not snap or snap["snapshot_id"] != payload.get("sid"):
            return self._empty_wrapper(
                "unavailable", module, [expired],
                self._snap_block(scope, None, source),
                self._freshness(self._health_row(acct, module, source), None, now, source),
                self._source_refs(None, source, safe_url, parser_version, evidence_ref))
        off = payload.get("off", 0)
        if not isinstance(off, int) or off < 0:
            off = 0
        items = snap["items"]
        if kind == "events":
            items = self._events_with_status(items, now)
        page, has_more, next_cursor = self._page(acct, module, scope, snap["snapshot_id"],
                                                 items, off, limit)
        issue_c = self._completeness_issue_for(module, snap)
        # 游标语义（交接 2.13）：events 不完整快照（截断/有缺口）的 cursor 保持 partial；
        # 课表 complete 恒 false 属既有设计，不机械跟随，仍为 ok。
        # R33：jwweb 部分快照（missing_ranges 非空）的 cursor 页同样保持 partial，
        # 不因翻页把部分结果洗成 ok。
        if kind in ("events", "mypage") and not snap.get("complete"):
            status = "partial"
        elif kind == "jwweb":
            # R33（§2.27③④）：partial 语义统一——快照登记缺口，或最近一次观测为
            # partial（健康行 last_issue=completeness_unproven 且尚未被成功覆盖），
            # cursor 页均保持 partial，不因翻页洗成 ok
            health = self._health_row(acct, module, source)
            last_issue = health.get("last_issue") if health else None
            partial_observed = bool(snap.get("missing_ranges")) or (
                isinstance(last_issue, dict)
                and last_issue.get("code") == "completeness_unproven")
            status = "partial" if partial_observed else "ok"
            if partial_observed:
                issue_c = E.issue("completeness_unproven", False,
                                  "该范围最近一次观测存在缺口（来源分页不完整），"
                                  "本次为已保存快照的继续读取")
        else:
            status = "ok"
        # R33（§2.27③）：jwweb partial 语义时 freshness 的 last_success 用健康行旧值，
        # 不用 partial 快照的 fetched_at 冒充
        if kind in ("jwweb", "mypage", "events") and status == "partial":
            health = self._health_row(acct, module, source)
            old_ls = health.get("last_success_at") if health else None
            freshness = {
                "last_attempt_at": now.isoformat(timespec="seconds"),
                "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(now, old_ls),
                "is_stale": None,
                "by_source": [{"source_id": source, "last_attempt_at": now.isoformat(timespec="seconds"),
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None}],
            }
        else:
            freshness = self._freshness(self._health_row(acct, module, source), snap, now,
                                        source)
        return {
            "status": status,
            "module_id": module,
            "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": freshness,
            "source_refs": self._source_refs(snap, source, safe_url,
                                             parser_version, evidence_ref),
            "issues": [i for i in [issue_c] if i],
        }

    def _completeness_issue_for(self, module: str, snap: dict) -> dict | None:
        """快照 complete=false 时的完整性说明；complete=true 时返回 None。"""
        if snap.get("complete"):
            return None
        if module == MODULE_TIMETABLE:
            return self._completeness_issue_timetable(len(snap.get("items", [])))
        missing = "；".join(snap.get("missing_ranges") or []) or "（未记录具体范围）"
        return E.issue(E.COMPLETENESS_UNPROVEN, False,
                       "部分日期未读完（%s）；覆盖范围为已读部分，完整性不成立" % missing)

    @staticmethod
    def _events_with_status(entries: list, now: datetime) -> list:
        out = []
        for e in entries:
            item = dict(e)
            item["status"] = derive_status(e, now)
            out.append(item)
        return out

    def _completeness_issue_timetable(self, total: int) -> dict:
        return E.issue(
            E.COMPLETENESS_UNPROVEN, False,
            "本次来源返回 %d 条；服务端未提供总数或分页结束证据，完整性待证（不视为全量，"
            "coverage.complete 不置 true）" % total)

    # ---------- 工具 4：来源健康 ----------
    def get_source_health(self, module_id: str | None = None) -> dict:
        now = self._now_fn()
        if module_id and module_id not in KNOWN_MODULE_IDS:
            return {
                "status": "unavailable",
                "generated_at": now.isoformat(timespec="seconds"),
                "account_scope_id": None,
                "session": {"present": False, "user_type": None, "created_at": None},
                "sources": [],
                "issues": [E.issue(E.INVALID_ARGUMENTS, False,
                                   "未知 module_id：%s" % module_id)],
            }
        acct, session = self._ctx()
        rows = []
        if acct:
            rows += self.store.get_health_rows(acct, module_id)
        rows += self.store.get_health_rows(ACCOUNT_PUBLIC, module_id)  # 公共来源行
        sources = []
        for r in rows:
            age = _age_seconds(now, r["last_success_at"])
            last_issue = r["last_issue"] or {}
            sources.append({
                "account_scope_id": r.get("account_scope_id", ACCOUNT_PUBLIC),
                "source_id": r["source_id"],
                "module_id": r["module_id"],
                "last_attempt_at": r["last_attempt_at"],
                "last_success_at": r["last_success_at"],
                "data_age_seconds": age,
                "is_stale": None,
                "last_issue_code": last_issue.get("code"),
                "last_issue_message": last_issue.get("safe_message"),
                "last_attempt_query_scope": r["last_query_scope"],
            })
        return {
            "status": "ok",
            "generated_at": now.isoformat(timespec="seconds"),
            "account_scope_id": acct,
            "session": {
                "present": bool(session),
                "user_type": session.get("user_type") if session else None,
                "created_at": session.get("created_at") if session else None,
            },
            "sources": sources,
            "issues": [],
        }
