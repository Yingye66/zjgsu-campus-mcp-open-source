# -*- coding: utf-8 -*-
"""教务网页版（jwglxt）应用服务：考试/成绩/网页课表三个只读工具的编排。

与 campus_service.CampusService 同一套包装/缓存/游标语义（继承其内部工具）：
- 账号域：本机会话中的 jwglxt_cookies（login_cli init 时用同一凭据顺带建立；密码不落盘）；
- 会话过期（session_expired）→ status=unavailable + auth_required，
  提示重新运行 python -m src.auth.login_cli init（与课表 unavailable 语义一致）；
- 成功（含合法空结果）→ 写快照 + last_success_at；失败只记 attempt/issue，旧快照不变；
- xnm（学年，4 位数字）与 xqm（学期码：3/12/16）严格校验（取证记录 §四 的实测参数域）；
- complete 恒为 false：来源未提供总数/结束证据，返回条数不代表该学期全量
  （与课表 completeness_unproven 同语义，见交接 2.13）。
"""
from __future__ import annotations

import hashlib
import functools
import threading
import uuid

from ..auth import session as S

import io
import json
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.campus.jwweb import (JwwebTransport, JWXT_BASE, EXAM_PATH, GRADE_PATH,
                                     XY_PATH, XY_GNM,
                                     fetch_academic_progress,
                                     TIMETABLE_PATH, EXAM_GNM, GRADE_GNM, TIMETABLE_GNM,
                                     GRADE_MX_GNM, GRADE_KC_PATH, SX_GNM, SX_PATH,
                                     PYFAWJ_GNM, PYFAWJ_PATH, JXZXJH_GNM, JXZXJH_PATH,
                                     JXZXJHKC_PATH, JXZXJHKC_INDEX_PATH,
                                     CXKCCX_GNM, CXKCCX_PATH,
                                     WSKCDZ_GNM, WSKCDZ_PATH,
                                     SKDG_GNM, SKDG_PATH,
                                     SZF_GNM, SZF_PATH,
                                     KXJS_GNM, KXJS_PATH,
                                     BJKB_GNM, BJKB_PATH,
                                     YJCL_GNM, YJCL_PATH,
                                     XJYJ_GNM, XJYJ_INDEX_PATH, XJYJ_JDLB_PATH,
                                     GRXX_GNM, GRXX_PATH,
                                     _EXAM_FIELDS, _GRADE_FIELDS, _KB_FIELDS,
                                     _GRADE_COURSE_FIELDS, _GRADE_COMPONENT_FIELDS,
                                     _INTERNSHIP_FIELDS, _TRAINING_PLAN_FIELDS,
                                     _TEACHING_PLAN_FIELDS, _TEACHING_PLAN_COURSE_FIELDS,
                                     _RETAKE_FIELDS, _ONLINE_URL_FIELDS,
                                     _SYLLABUS_FIELDS,
                                     _map_items, fetch_exams, fetch_grades,
                                     fetch_grade_courses, fetch_grade_components,
                                     fetch_internship_grades, fetch_training_plans,
                                     fetch_training_plan_doc, PYFAXXWH_PATH,
                                     fetch_teaching_plans, fetch_teaching_plan_courses,
                                     fetch_retake_courses, fetch_academic_warnings,
                                     fetch_credit_requirements, fetch_my_profile,
                                     fetch_online_class_urls, fetch_syllabus_list,
                                     fetch_innovation_scores, fetch_free_classrooms, sanitize_url,
                                     fetch_class_timetable, fetch_current_term,
                                     resolve_class_bh_id,
                                     fetch_class_timetable, fetch_current_term,
                                     resolve_class_bh_id,
                                     fetch_timetable_web)
from ..domain import errors as E
from ..domain.models import (ACCOUNT_PUBLIC, MODULE_APPROVALS,
                             MODULE_AWARDS_PUBLICATIONS, MODULE_CLASS_TIMETABLE,
                             MODULE_COURSE_CATALOG, MODULE_DIGITAL_RESOURCES,
                             MODULE_PRESELECT_INFO,
                             MODULE_LIBRARY_SEARCH,
                             MODULE_FREE_CLASSROOMS, MODULE_ACADEMIC,
                             MODULE_ACADEMIC_WARNINGS, MODULE_EXAM_REGISTRATIONS,
                             MODULE_CREDIT_REQUIREMENTS, MODULE_MY_PROFILE,
                             MODULE_ONLINE_URLS, MODULE_SYLLABUS,
                             MODULE_INNOVATION,
                             MODULE_EXAMS, MODULE_GRADES,
                             MODULE_RETAKE_COURSES, MODULE_TEACHING_PLAN,
                             MODULE_TEACHING_PLAN_COURSES,
                             MODULE_TIMETABLE_WEB, MODULE_GRADE_DETAILS,
                             MODULE_INTERNSHIP, MODULE_TRAINING_PLAN,
                             MODULE_TRAINING_PLAN_DOC, SOURCE_JWGLXT,
                             SOURCE_FLOW, SOURCE_XSC_PUB, SOURCE_LIB)
from .campus_service import CampusService, _age_seconds, _encode_cursor
from .request_timing import TimingTransport
from .semester_credits import summarize_timetable_credits

PARSER_VERSION_JWWEB = "jwweb-parser/0.1.0"
EVIDENCE_REF_JWWEB = "validation/jwweb/取证记录.md（2026-10-13 真实数据取证：考试/成绩/课表）"


def _sha12(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


_PLAN_DOC_LOCK = threading.Lock()


def _serialized_plan_doc(fn):
    """培养方案附件下载+发布串行化（同/不同身份都不交叉写同一目录）。

    低频操作；用一把模块锁换取确定性的发布顺序，避免共享 .tmp/.bak 冲突。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _PLAN_DOC_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _grade_parts(raw) -> list[str]:
    return [p for p in re.split(r"[,，;；\s]+", str(raw or "").strip()) if p]


def _row_applies_to_grade(row: dict, grade: str) -> tuple[bool, str]:
    """按源真实字段判定适用年级。

    - 主字段 njdm_id（实测为逗号分隔多值）；synj 仅作回退，不作为主口径；
    - 两字段同时存在且完全不重叠 → 冲突，拒绝；
    - 缺失/空/格式异常 → 拒绝，不把请求年级重新标注给方案。
    """
    nj = str(row.get("njdm_id") or "").strip()
    syn = str(row.get("synj") or "").strip()
    if not nj and not syn:
        return False, "缺少 njdm_id/synj"
    nj_parts = _grade_parts(nj)
    syn_parts = _grade_parts(syn)
    if nj and nj_parts and any(not re.fullmatch(r"[0-9]{4}", p) for p in nj_parts):
        return False, "njdm_id 含非四位年级 token（%s）" % nj
    if syn and syn_parts and any(not re.fullmatch(r"[0-9]{4}", p) for p in syn_parts):
        return False, "synj 含非四位年级 token（%s）" % syn
    if nj and syn and nj_parts and syn_parts:
        if not (set(nj_parts) & set(syn_parts)):
            return False, "njdm_id 与 synj 冲突（%s / %s）" % (nj, syn)
    if nj:
        if not nj_parts:
            return False, "njdm_id 为空或格式异常"
        return (grade in nj_parts), "njdm_id=%s" % nj
    if not syn_parts:
        return False, "synj 为空或格式异常"
    return (grade in syn_parts), "synj=%s" % syn


def _extract_docx_text(path: Path) -> tuple[str | None, bool]:
    """按正文顺序提取 docx 文本（段落与表格交错），返回 (text, complete)。

    - python-docx 缺失或文件不可读 → (None, False)，调用方不得声称有全文；
    - 表格解析异常 → complete=False，但仍返回已取到的部分；
    - 顺序以 word/document.xml 的 body 子元素为准，不再「段落全部在前、表格全部在后」。
    """
    try:
        import docx  # 可选依赖；缺席不影响 docx 本身落盘
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except Exception:
        return None, False
    try:
        d = docx.Document(str(path))
    except Exception:
        return None, False
    parts: list[str] = []
    complete = True
    w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def walk(container) -> None:
        nonlocal complete
        for child in container.iterchildren():
            tag = child.tag.split("}")[-1]
            if tag == "p":
                text = Paragraph(child, d).text.strip()
                if text:
                    parts.append(text)
            elif tag == "tbl":
                try:
                    table = Table(child, d)
                    for row in table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        if any(cells):
                            parts.append(" | ".join(cells))
                except Exception:
                    complete = False
            elif tag == "sdt":
                # 内容控件：按 sdtContent 递归；无内容但有文字 → 不完整
                content = child.find(w_ns + "sdtContent")
                if content is None:
                    if any((t.text or "").strip()
                           for t in child.findall(".//" + w_ns + "t")):
                        complete = False
                else:
                    walk(content)
            elif tag in ("bookmarkStart", "bookmarkEnd",
                         "commentRangeStart", "commentRangeEnd"):
                continue
            else:
                # 未知块级节点：若含文字，不能声称可靠全文
                if any((t.text or "").strip()
                       for t in child.findall(".//" + w_ns + "t")):
                    complete = False

    try:
        walk(d.element.body)
    except Exception:
        return None, False
    return "\n".join(parts), complete


_DOCX_REQUIRED = ("[Content_Types].xml", "word/document.xml")


def _verify_docx_bytes(body: bytes) -> tuple[bool, str]:
    """发布前校验：ZIP 结构 + DOCX 必要成员 + python-docx 可读性。

    只验 PK 头不足以排除「截断/伪造/HTML 换扩展名」，必须完整校验后才允许覆盖旧件。
    """
    if not body or body[:4] != b"PK\x03\x04":
        return False, "非 ZIP 格式（PK 头不符）"
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            bad = z.testzip()
            if bad:
                return False, "ZIP 校验失败（成员 %s CRC 异常）" % bad
            names = set(z.namelist())
    except Exception as e:
        return False, "ZIP 打开失败（%s）" % type(e).__name__
    missing = [n for n in _DOCX_REQUIRED if n not in names]
    if missing:
        return False, "缺少 DOCX 必要成员：%s" % ", ".join(missing)
    try:
        import docx
    except Exception:
        return True, "ZIP 与必要成员通过（python-docx 缺席，未做可读性校验）"
    try:
        docx.Document(io.BytesIO(body))
    except Exception as e:
        return False, "DOCX 打开失败（%s）" % type(e).__name__
    return True, ""


_XNM_RE = re.compile(r"\d{4}")
_XNM_RE_FULL = re.compile(r"\A[0-9]{4}\Z")  # R31：完整匹配 ASCII 四位数字（拒绝换行/空白/Unicode 数字）
_XQM_ALLOWED = ("3", "12", "16")  # 正方学期码：3=第一学期(秋) 12=第二学期(春) 16=短学期（实测口径）


class JWWebService(CampusService):
    """教务网页版工具服务。store/data_dir 与 CampusService 共用同一套缓存与健康记录。

    jwweb_transport：注入点（测试用假传输；生产为 None → 按需创建真实 JwwebTransport）。
    """
    def __init__(self, store, data_dir, transport=None, now_fn=None, jwweb_transport=None):
        super().__init__(store, data_dir, transport=transport, now_fn=now_fn)
        self._jwweb_transport = jwweb_transport

    # ---------- 内部 ----------

    def _jwweb_ctx(self, module: str, source: str, scope: str):
        """返回 (acct, cookies, transport, err_wrapper)。err_wrapper 非 None 表示应直接返回。"""
        acct, session = self._ctx()
        if not acct or not session:
            return acct, None, None, self._unavailable(
                module, source, scope,
                E.issue(E.AUTH_REQUIRED, False,
                        "本地无有效会话：请先在本机运行登录初始化"
                        "（python -m src.auth.login_cli init）"),
                JWXT_BASE + EXAM_PATH, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        cookies = session.get("jwglxt_cookies")
        # R26 剩余：只接受完整属性列表；旧 dict/缺域格式一律拒绝并提示重新登录
        # （不自动迁移——CAS SSO cookie 不得被泛化到教务域）
        if not isinstance(cookies, list) or not cookies:
            # 旧 dict 格式 / 空值：区分提示（旧格式说明会话文件来自旧版本 init）
            fmt = "格式过旧（旧版 dict 结构）" if isinstance(cookies, dict) else "为空"
            return acct, None, None, self._unavailable(
                module, source, scope,
                E.issue(E.AUTH_REQUIRED, False,
                        "教务网页版会话%s：请重新运行 "
                        "python -m src.auth.login_cli init 生成完整格式会话" % fmt),
                JWXT_BASE + EXAM_PATH, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        # R32：生产路径每次调用新建 transport（全新 jar，只含本次恢复的会话 cookie，
        # 不累积上一账号/上一会话的旧 cookie）；注入实例仅供测试假传输。
        if self._jwweb_transport is not None:
            t = self._jwweb_transport
        else:
            t = JwwebTransport()
        try:
            t.set_cookies(cookies)
        except ValueError as e:
            return acct, None, None, self._unavailable(
                module, source, scope,
                E.issue(E.AUTH_REQUIRED, False, "%s" % e),
                JWXT_BASE + EXAM_PATH, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        return acct, cookies, t, None

    @staticmethod
    def _validate_xnxq(module: str, source: str, scope: str, xnm, xqm):
        if not isinstance(xnm, str) or not _XNM_RE_FULL.match(xnm):
            return E.issue(E.INVALID_ARGUMENTS, False,
                           "xnm 须为 4 位数字学年（如 2025 表示 2025-2026 学年）")
        if not isinstance(xqm, str) or xqm not in _XQM_ALLOWED:
            return E.issue(E.INVALID_ARGUMENTS, False,
                           "xqm 须为学期码：3=第一学期，12=第二学期，16=短学期")
        return None

    def _serve_jwweb(self, module: str, fetch, field_map: dict,
                     xnm: str, xqm: str, cursor: str | None, limit: int) -> dict:
        """三个模块共用的编排主体：校验 → 会话 → 抓取 → 快照/游标 → 包装。"""
        source = SOURCE_JWGLXT
        scope = "%s-%s" % (xnm, xqm)
        safe_url = {
            MODULE_EXAMS: JWXT_BASE + EXAM_PATH,
            MODULE_GRADES: JWXT_BASE + GRADE_PATH,
            MODULE_TIMETABLE_WEB: JWXT_BASE + TIMETABLE_PATH,
            MODULE_INTERNSHIP: JWXT_BASE + SX_PATH,
            MODULE_TRAINING_PLAN: JWXT_BASE + PYFAWJ_PATH,
            MODULE_TEACHING_PLAN: JWXT_BASE + JXZXJH_PATH,
            MODULE_TEACHING_PLAN_COURSES: JWXT_BASE + JXZXJHKC_PATH,
            MODULE_RETAKE_COURSES: JWXT_BASE + CXKCCX_PATH,
            MODULE_ACADEMIC_WARNINGS: JWXT_BASE + YJCL_PATH,
            MODULE_CREDIT_REQUIREMENTS: JWXT_BASE + XJYJ_JDLB_PATH,
            MODULE_MY_PROFILE: JWXT_BASE + GRXX_PATH,
            MODULE_ONLINE_URLS: JWXT_BASE + WSKCDZ_PATH,
            MODULE_SYLLABUS: JWXT_BASE + SKDG_PATH,
            MODULE_INNOVATION: JWXT_BASE + SZF_PATH,
            MODULE_FREE_CLASSROOMS: JWXT_BASE + KXJS_PATH,
            MODULE_CLASS_TIMETABLE: JWXT_BASE + BJKB_PATH,
        }[module] + "?gnmkdm=" + {MODULE_EXAMS: EXAM_GNM, MODULE_GRADES: GRADE_GNM,
                                  MODULE_TIMETABLE_WEB: TIMETABLE_GNM,
                                  MODULE_INTERNSHIP: SX_GNM,
                                  MODULE_TRAINING_PLAN: PYFAWJ_GNM,
                                  MODULE_TEACHING_PLAN: JXZXJH_GNM,
                                  MODULE_TEACHING_PLAN_COURSES: JXZXJH_GNM,
                                  MODULE_RETAKE_COURSES: CXKCCX_GNM,
                                  MODULE_ACADEMIC_WARNINGS: YJCL_GNM,
                                  MODULE_CREDIT_REQUIREMENTS: XJYJ_GNM,
                                  MODULE_MY_PROFILE: GRXX_GNM,
                                  MODULE_ONLINE_URLS: WSKCDZ_GNM,
                                  MODULE_SYLLABUS: SKDG_GNM,
                                  MODULE_INNOVATION: SZF_GNM,
                                  MODULE_FREE_CLASSROOMS: KXJS_GNM,
                                  MODULE_CLASS_TIMETABLE: BJKB_GNM}[module]
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        bad = self._validate_xnxq(module, source, scope, xnm, xqm)
        if bad:
            return self._unavailable(module, source, scope, bad,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)

        # R30：与会话门禁顺序对齐原课表——先验证本人会话存在，再处理游标
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope,
                                      cursor, limit, self._now_fn(),
                                      safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")

        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err

        fetch_result = fetch(transport, xnm, xqm)
        done_at = self._now_fn()
        now_s = done_at.isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        old = self.store.get_snapshot(acct, module, source, scope)

        # R28：失败路径必须登记 last_issue（白名单 code），保留最后成功时间不变
        def _record_issue_if_failed(code: str, msg: str, retryable: bool):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue(code, retryable, msg))

        # ---- 失败分支（R27：有旧快照时复用旧条目/分页/快照id/freshness，不再返回空列表） ----
        if fetch_result.outcome == "session_expired":
            issue = E.issue(E.AUTH_REQUIRED, False,
                            "教务网页版会话已失效：%s；请重新运行 "
                            "python -m src.auth.login_cli init 刷新" % fetch_result.diag)
            _record_issue_if_failed(E.AUTH_REQUIRED, "jwglxt 会话失效", False)
            if old:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"], old.get("items") or [], 0, limit)
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": self._freshness_jwweb(acct, module, source,
                                                       old, done_at),
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [issue],
                }
            return self._empty_wrapper(
                "unavailable", module, [issue],
                self._snap_block(scope, None, source),
                self._freshness(self._health_row(acct, module, source), None, done_at, source),
                self._source_refs(None, source, safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB))
        if fetch_result.outcome in ("http_error", "network_error", "parse_error",
                                    "upstream_error", "unexpected"):
            issue = E.issue("upstream_error", fetch_result.outcome == "network_error",
                            "教务读取失败（%s：%s）" % (fetch_result.outcome, fetch_result.diag))
            _record_issue_if_failed("upstream_error",
                                    "jwglxt %s" % fetch_result.outcome,
                                    fetch_result.outcome == "network_error")
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"], old["items"], 0, limit)
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": self._freshness_jwweb(acct, module, source,
                                                       old, done_at),
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [issue],
                }
            return self._empty_wrapper(
                "unavailable", module, [issue],
                self._snap_block(scope, old, source),
                self._freshness(self._health_row(acct, module, source), old, done_at, source),
                self._source_refs(old, source, safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB))

        # ok / partial_source / ok_empty：白名单映射（整批拒绝，不产出半成品）
        mapped, violations = _map_items(fetch_result.items or [], field_map)
        if mapped is None:
            # R27 剩余：映射失败与其他失败同路径——有旧快照则复用旧条目（partial）
            issue = E.issue("upstream_error", False,
                            "来源字段不合规（%d 项）：%s" % (
                                len(violations), "；".join(violations[:6])))
            _record_issue_if_failed("upstream_error", "jwglxt 字段不合规", False)
            if old is not None:
                # R27 剩余：按快照存在判断（合法空快照同样复用），不按 items 非空
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"], old.get("items") or [], 0, limit)
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": self._freshness_jwweb(acct, module, source,
                                                       old, done_at),
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [issue],
                }
            return self._unavailable(module, source, scope, issue,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        # 学年/学期冗余写入条目（便于跨学期聚合时溯源）
        for row in mapped:
            if module == MODULE_TIMETABLE_WEB:
                # All timetable scalar fields have a string-or-null output contract.
                for field in field_map.values():
                    if row.get(field) is not None:
                        row[field] = str(row[field])
            row["academic_year"] = xnm
            row["semester"] = xqm

        partial = fetch_result.outcome == "partial_source"
        issues = []
        if fetch_result.extras.get("sjk_count"):
            # R23：伴随实践课块只登记，不并入主 schema、不冒充已覆盖
            issues.append(E.issue("adjacent_data_omitted", False,
                                  "另有实践环节课块 sjkList=%d 条（起止周形式，字段结构独立，"
                                  "未并入本工具输出）" % fetch_result.extras["sjk_count"]))
        if fetch_result.extras.get("sanitized_links"):
            # R41/P1：链接净化的具名说明
            issues.append(E.issue("credentials_sanitized", False,
                                  "%d 条记录的链接含认证类参数，已净化（存储与输出均为净化值）"
                                  % fetch_result.extras["sanitized_links"]))
        # §2.41 R34/R35/R36：统一收尾契约（partial 保留旧成功/缺口/游标、时间分离）
        return self._finalize_jwweb_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, partial=partial, partial_diag=fetch_result.diag,
            total_expected=fetch_result.total_expected,
            empty_evidence=(fetch_result.outcome == "ok_empty"),
            issues=issues, now_s=now_s, done_at=done_at, limit=limit)

    def _freshness_jwweb(self, acct: str, module: str, source: str,
                         snap: dict | None, now: datetime) -> dict:
        """R33（§2.27③）：回退路径的 freshness——快照为 partial（缺口登记）时
        last_success 用健康行旧值，不用 partial 快照 fetched_at 冒充。"""
        if snap and snap.get("missing_ranges"):
            health = self._health_row(acct, module, source)
            old_ls = health.get("last_success_at") if health else None
            return {
                "last_attempt_at": health.get("last_attempt_at") if health else None,
                "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(now, old_ls),
                "is_stale": None,
                "by_source": [{"source_id": source,
                               "last_attempt_at": health.get("last_attempt_at") if health else None,
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(now, old_ls),
                               "is_stale": None}],
            }
        return self._freshness(self._health_row(acct, module, source), snap, now, source)

    # ---------- 工具：考试 ----------

    def _finalize_jwweb_success(self, *, acct: str, module: str, source: str,
                                scope: str, safe_url: str, mapped: list,
                                partial: bool, partial_diag: str | None,
                                total_expected=None, empty_evidence: bool = False,
                                missing_extra: list | None = None,
                                issues: list | None = None,
                                now_s: str, done_at, limit: int) -> dict:
        """§2.41 R34/R35/R36 共用收尾契约（原 _serve_jwweb 逻辑抽出，供全部编排复用）：

        - partial：只要存在旧成功快照就不覆盖（返回旧条目+缺口），不推进
          last_success、不清空健康问题；本次部分观测不入快照，缺口登记健康+issues；
          无旧快照时 upsert 本次部分观测（missing_ranges 登记缺口），freshness 分离；
        - 全量/合法空：upsert + record_success，常规 freshness；
        - complete 恒 false。mapped 必须已通过白名单映射（坏行由调用方先具名）。
        """
        issues = list(issues or [])
        old = self.store.get_snapshot(acct, module, source, scope)
        if partial:
            gap_msg = "来源分页不完整：%s" % (partial_diag or "")
            issues.append(E.issue("completeness_unproven", True, gap_msg))
            if old is not None:
                # R35：保留旧成功版本，本次部分观测不入库
                self.store.record_issue(
                    acct, module, source, now_s, scope,
                    E.issue("completeness_unproven", True,
                            "本次部分观测（%d条，来源声明%s条）未入库；保留既有成功快照（%d条）"
                            % (len(mapped), total_expected, len(old.get("items") or []))))
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                issues.append(E.issue(
                    "completeness_unproven", False,
                    "已返回既有成功快照（%d条）；本次部分观测未入库，缺口见健康记录"
                    % len(old.get("items") or [])))
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": {
                        "last_attempt_at": now_s, "last_success_at": old_ls,
                        "data_age_seconds": _age_seconds(done_at, old_ls),
                        "is_stale": None,
                        "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                       "last_success_at": old_ls,
                                       "data_age_seconds": _age_seconds(done_at, old_ls),
                                       "is_stale": None}],
                    },
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": issues,
                }
        missing = []
        if partial:
            missing = ["%s（已取得%d/%s条）" % (scope, len(mapped), total_expected)]
        if missing_extra:
            missing = missing + list(missing_extra)
        if partial:
            # R33：部分观测不算成功——不推进 last_success、登记缺口；与快照同一事务
            snap = self.store.upsert_snapshot_and_health(
                acct, module, source, scope, items=mapped, fetched_at=now_s,
                complete=False, consistency="best_effort", missing_ranges=missing,
                empty_evidence=empty_evidence, success=False, at=now_s,
                issue=E.issue("completeness_unproven", True, gap_msg))
        else:
            snap = self.store.upsert_snapshot_and_health(
                acct, module, source, scope, items=mapped, fetched_at=now_s,
                complete=False, consistency="best_effort", missing_ranges=missing,
                empty_evidence=empty_evidence, success=True, at=now_s)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], mapped, 0, limit)
        if partial:
            health_row = self._health_row(acct, module, source)
            old_ls = health_row.get("last_success_at") if health_row else None
            freshness = {
                "last_attempt_at": now_s, "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(done_at, old_ls), "is_stale": None,
                "by_source": [{"source_id": source, "last_attempt_at": now_s,
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(done_at, old_ls),
                               "is_stale": None}],
            }
        else:
            freshness = self._freshness(self._health_row(acct, module, source), snap,
                                        done_at, source)
        return {
            "status": "partial" if partial else "ok",
            "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": freshness,
            "source_refs": self._source_refs(snap, source, safe_url,
                                             PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
            "issues": issues,
        }

    def get_exams(self, xnm: str, xqm: str, cursor: str | None = None, limit: int = 50) -> dict:
        return self._serve_jwweb(MODULE_EXAMS, fetch_exams, _EXAM_FIELDS,
                                 xnm, xqm, cursor, limit)

    # ---------- 工具：成绩 ----------

    def get_grades(self, xnm: str, xqm: str, cursor: str | None = None, limit: int = 50) -> dict:
        return self._serve_jwweb(MODULE_GRADES, fetch_grades, _GRADE_FIELDS,
                                 xnm, xqm, cursor, limit)

    # ---------- 工具：网页课表 ----------

    def get_timetable_web(self, xnm: str, xqm: str, cursor: str | None = None, limit: int = 50) -> dict:
        started = time.perf_counter()
        account, _session = self._ctx()
        requests = []
        fetch_started = fetch_finished = None

        def measured_fetch(transport, year, semester):
            nonlocal fetch_started, fetch_finished
            timed = TimingTransport(transport)
            fetch_started = time.perf_counter()
            try:
                return fetch_timetable_web(timed, year, semester)
            finally:
                fetch_finished = time.perf_counter()
                requests.extend(timed.requests)

        result = self._serve_jwweb(MODULE_TIMETABLE_WEB, measured_fetch, _KB_FIELDS,
                                   xnm, xqm, cursor, limit)
        served = time.perf_counter()
        if result["status"] != "unavailable":
            block = result["coverage"]["source_snapshots"][0]
            snapshot = self.store.get_snapshot(account, MODULE_TIMETABLE_WEB,
                                               SOURCE_JWGLXT, block["query_scope"]) if account else None
            whole = bool(snapshot and snapshot["snapshot_id"] ==
                         result["pagination"]["result_snapshot_id"])
            # Aggregate the same full local snapshot before result pagination.
            rows = snapshot["items"] if whole else result["items"]
            result["credit_summary"] = summarize_timetable_credits(
                rows, complete=block["complete"] and result["status"] == "ok",
                has_more=(not whole and result["pagination"]["has_more"]), issues=result["issues"])
            if not whole:
                result["credit_summary"]["notes"].append(
                    "未能核验相同完整本地快照，仅汇总本页条目。")

        finished = time.perf_counter()
        context_ms = ((fetch_started or served) - started) * 1000
        fetch_ms = ((fetch_finished - fetch_started) * 1000
                    if fetch_started is not None and fetch_finished is not None else 0)
        http_ms = sum(row["elapsed_ms"] for row in requests)
        total_ms = (finished - started) * 1000
        result["performance"] = {"source_query": {
            "mode": "network_attempt" if requests else ("local_snapshot" if cursor else "local_rejection"),
            "total_ms": round(total_ms, 3), "context_ms": round(context_ms, 3),
            "http_ms": round(http_ms, 3), "parse_ms": round(max(0, fetch_ms - http_ms), 3),
            "local_result_ms": round(max(0, total_ms - context_ms - fetch_ms), 3),
            "http_request_count": len(requests), "requests": requests,
        }}
        return result

    def get_training_plans(self, year: str, keyword: str | None = None,
                           cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30 第 3 项：get_training_plans。培养方案文件（按适用年级）。
        year=适用年级（如 2025，来源 synj 过滤）；keyword=名称/专业子串过滤（客户端）。"""
        module, source = MODULE_TRAINING_PLAN, SOURCE_JWGLXT
        kw = (keyword or "").strip().lower()
        scope = "plans-%s:%s" % (year, kw if kw else "ALL")
        safe_url = JWXT_BASE + PYFAWJ_PATH + "?gnmkdm=" + PYFAWJ_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if not isinstance(year, str) or not re.fullmatch(r"[0-9]{4}", year):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "year 须为 4 位适用年级（如 2025），用于来源过滤"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_training_plans(transport, year)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % fr.diag),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "培养方案文件读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        rows = fr.items or []
        if kw:
            rows = [c for c in rows
                    if kw in str(c.get("pyfamc", "")).lower()
                    or kw in str(c.get("zymc", "")).lower()]
        # R34：映射失败走具名错误路径（有旧成功按契约回退），不静默转空
        mapped, violations = _map_items(rows, _TRAINING_PLAN_FIELDS,
                                        require_course_name=False)
        if mapped is None:
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", False, "jwglxt 字段不合规"))
            issue = E.issue("upstream_error", False,
                            "来源字段不合规（%d 项）：%s" % (
                                len(violations), "；".join(violations[:6])))
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": self._freshness_jwweb(acct, module, source,
                                                       old, self._now_fn()),
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [issue],
                }
            return self._unavailable(module, source, scope, issue,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        mapped = [m for m in mapped if m.get("plan_name")]  # 无名称行跳过（具名于 diag）
        for row in mapped:
            row["entry_id"] = row.get("plan_ref") or ""
        # §2.41 R34/R35/R36：统一收尾契约
        return self._finalize_jwweb_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, partial=(fr.outcome == "partial_source"),
            partial_diag=fr.diag, total_expected=fr.total_expected,
            empty_evidence=(fr.outcome == "ok_empty"),
            missing_extra=(["plans-%s 课程列表不完整" % year]
                           if fr.outcome == "partial_source" else []),
            issues=[E.issue("completeness_unproven", False,
                            "附件正文下载未接入（has_attachment 仅标志）")],
            now_s=now_s, done_at=self._now_fn(), limit=limit)

    # ---------- 培养方案附件：路径 / 元数据 / 保旧 ----------

    def _plan_doc_paths(self, acct: str, grade: str, name: str) -> dict:
        """文件键=账号域+年级+方案名+逻辑身份哈希；显示名只供阅读。

        仅清洗显示名可能让 `A/B` 与 `A:B` 落到同一路径，因此追加
        _sha12(acct, grade, name) 作为稳定身份；不使用每次变化的会话附件 id。
        """
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)[:40]
        stem = "%s__%s" % (safe, _sha12(acct, grade, name))
        base = (Path(self.data_dir) / "training_plan_docs"
                / (acct or "acct-unknown") / str(grade))
        return {"dir": base,
                "docx": base / ("%s.docx" % stem),
                "txt": base / ("%s.txt" % stem),
                "meta": base / ("%s.meta.json" % stem)}

    @staticmethod
    def _read_plan_meta(meta_path: Path) -> dict | None:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _sha256_file(path: Path) -> str | None:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    def _stale_plan_entry(self, meta: dict, paths: dict,
                          expected_version: str | None = None) -> dict | None:
        """只认「元数据匹配 + 文件哈希一致」的旧件；同名文件存在不等于可保旧。"""
        if expected_version is not None and meta.get("version") != expected_version:
            return None
        docx_path = paths["docx"]
        if not docx_path.is_file():
            return None
        if meta.get("sha256") != self._sha256_file(docx_path):
            return None
        txt_path = Path(meta.get("text_path") or "") if meta.get("text_path") else None
        return {
            "entry_id": "pyfadoc-stale-%s" % _sha12(meta.get("plan_name", ""),
                                                    meta.get("apply_grade", ""),
                                                    meta.get("account", "")),
            "kind": "stale_file",
            "plan_name": meta.get("plan_name"),
            "apply_grade": meta.get("apply_grade"),
            "version": meta.get("version"),
            "total_credit": meta.get("total_credit"),
            "file_path": str(docx_path),
            "size_bytes": docx_path.stat().st_size,
            "content_type": "application/msword",
            "text_path": str(txt_path) if txt_path and txt_path.is_file() else None,
            "text_chars": meta.get("text_chars"),
            "text_preview": meta.get("text_preview"),
            "downloaded_at": meta.get("downloaded_at"),
            "sha256": meta.get("sha256"),
            "text_complete": meta.get("text_complete"),
            "source_id": meta.get("source_id"),
        }

    @_serialized_plan_doc
    def get_training_plan_doc(self, plan_name: str, grade: str) -> dict:
        """F04 扩展（2026-10-18）：get_training_plan_doc。培养方案附件正文下载。

        plan_name=方案名（与列表工具 pyfamc 精确匹配，如 2024示例专业）；
        grade=适用年级（synj 来源过滤，与 get_training_plans 同口径）。
        链路（PC 会话实测）：Index 激活 → 本会话列表拿动态 pyfaxx_id →
        pyfaxxwh_cxPyfaxxFj.html GET 二进制 → PK 校验 → 落盘 docx + txt。
        pyfaxx_id 动态生成（跨会话失效），故每次调用实时查列表，不信任历史 id。"""
        module, source = MODULE_TRAINING_PLAN_DOC, SOURCE_JWGLXT
        name = (plan_name or "").strip()
        scope = "plan-doc-%s:%s" % (grade, name)
        safe_url = JWXT_BASE + PYFAXXWH_PATH + "?gnmkdm=" + PYFAWJ_GNM
        if not isinstance(name, str) or not (2 <= len(name) <= 60):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "plan_name 须为 2..60 字符的方案名"
                                             "（如 2024示例专业；来自 get_training_plans 的 plan_name）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if not isinstance(grade, str) or not re.fullmatch(r"[0-9]{4}", grade):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "grade 须为 4 位适用年级（如 2025），与 get_training_plans 的 year 同口径"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        paths = self._plan_doc_paths(acct, grade, name)
        docx_path, txt_path, meta_path = paths["docx"], paths["txt"], paths["meta"]
        old_entry = None
        if docx_path.exists():
            meta = self._read_plan_meta(meta_path)
            # 旧件只有在本账号、本年级、本方案且哈希一致时才可保旧
            if (meta and meta.get("account") == acct
                    and meta.get("apply_grade") == grade
                    and meta.get("plan_name") == name):
                old_entry = self._stale_plan_entry(meta, paths)

        def _fail(issue, partial_diag=None):
            if old_entry is not None:
                return self._finalize_jwweb_success(
                    acct=acct, module=module, source=source, scope=scope,
                    safe_url=safe_url, mapped=[dict(old_entry)], partial=True,
                    partial_diag=partial_diag, total_expected=None,
                    empty_evidence=False, issues=[issue],
                    now_s=now_s, done_at=self._now_fn(), limit=1)
            return self._unavailable(module, source, scope, issue,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)

        fr = fetch_training_plans(transport, grade)
        if fr.outcome == "session_expired":
            return _fail(E.issue(E.AUTH_REQUIRED, False,
                                 "教务网页版会话已失效：%s；请重新运行 "
                                 "python -m src.auth.login_cli init 刷新" % fr.diag), fr.diag)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return _fail(E.issue("upstream_error", True,
                                 "培养方案列表读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                         fr.diag)
        rows = fr.items or []
        exact = [r for r in rows if str(r.get("pyfamc", "")).strip() == name]
        if not exact:
            near = [str(r.get("pyfamc")) for r in rows
                    if name in str(r.get("pyfamc", ""))][:5]
            return _fail(E.issue("upstream_error", False,
                                 "适用年级 %s 的方案列表（%d 条）中没有与 %r 完全同名的方案；"
                                 "相近条目：%s。请用 get_training_plans(year=%s) 核对原文；"
                                 "本工具不替用户选择相近方案"
                                 % (grade, len(rows), name,
                                    "、".join(near) if near else "无", grade)))
        if len(exact) > 1:
            names = ["%s（版本 %s）" % (str(r.get("pyfamc")).strip(),
                                        str(r.get("bbh") or "无"))
                     for r in exact][:6]
            return _fail(E.issue("upstream_error", False,
                                 "存在 %d 个同名方案，拒绝代替用户选择：%s"
                                 % (len(exact), "；".join(names))))
        row = exact[0]
        row_grade, grade_diag = _row_applies_to_grade(row, grade)
        if not row_grade:
            return _fail(E.issue("upstream_error", False,
                                 "方案 %r 的适用年级无法确认（%s）；"
                                 "不以请求年级 %s 重新标注"
                                 % (name, grade_diag, grade)))
        version = str(row.get("bbh") or "") or None
        if old_entry is not None:
            meta = self._read_plan_meta(meta_path)
            old_entry = self._stale_plan_entry(meta, paths, version)
        pid_raw = str(row.get("pyfaxx_id") or "")
        m = re.search(r"[0-9A-F]{32}", pid_raw)
        if not m:
            return _fail(E.issue("upstream_error", False,
                                 "列表行 pyfaxx_id 不含 32 位标识（%r）" % pid_raw[:60]))
        res = fetch_training_plan_doc(transport, m.group(0))
        if res["outcome"] == "session_expired":
            return _fail(E.issue(E.AUTH_REQUIRED, False,
                                 "教务网页版会话已失效：%s；请重新运行 "
                                 "python -m src.auth.login_cli init 刷新" % res["diag"]),
                         res["diag"])
        if res["outcome"] != "ok":
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", True,
                                            "pyfaxxwh %s" % res["outcome"]))
            return _fail(E.issue("upstream_error", True,
                                 "附件下载失败（%s：%s）" % (res["outcome"], res["diag"])),
                             res["diag"])
        # 成功：先完整校验，再写临时文件，最后原子发布；任何失败都保留旧版本
        body = res["body"]
        verified, why = _verify_docx_bytes(body)
        if not verified:
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", True,
                                            "docx 校验失败：%s" % why))
            return _fail(E.issue("upstream_error", True,
                                 "附件校验失败，未覆盖旧件（%s）" % why), why)
        token = uuid.uuid4().hex[:10]
        tmp_docx = docx_path.with_name("%s.tmp-%s" % (docx_path.name, token))
        tmp_txt = txt_path.with_name("%s.tmp-%s" % (txt_path.name, token))
        tmp_meta = meta_path.with_name("%s.tmp-%s" % (meta_path.name, token))
        backup_docx = docx_path.with_name("%s.bak-%s" % (docx_path.name, token))
        backup_txt = txt_path.with_name("%s.bak-%s" % (txt_path.name, token))
        had_docx = docx_path.is_file()
        had_txt = txt_path.is_file()
        backup_docx_sha = self._sha256_file(docx_path) if had_docx else None
        text = None
        text_complete = False
        try:
            out_dir = paths["dir"]
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp_docx.write_bytes(body)
            text, text_complete = _extract_docx_text(tmp_docx)
            if text is not None:
                tmp_txt.write_text(text, encoding="utf-8")
            meta = {
                "schema": 1,
                "account": acct,
                "apply_grade": grade,
                "plan_name": name,
                "version": str(row.get("bbh") or "") or None,
                "total_credit": str(row.get("zdxf") or "") or None,
                "pyfaxx_id": m.group(0),
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "text_chars": len(text) if text is not None else None,
                "text_complete": bool(text_complete),
                "text_preview": (text[:400] if text is not None else None),
                "text_path": (str(txt_path) if text is not None else None),
                "downloaded_at": now_s,
                "source_id": source,
            }
            tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            # 多文件不是单事务：先留备份，任一步失败按备份回滚
            if had_docx:
                shutil.copy2(docx_path, backup_docx)
            if had_txt:
                shutil.copy2(txt_path, backup_txt)
            # docx → txt → meta 依次替换；meta 最后落，哈希不匹配时不会被当作旧件
            os.replace(tmp_docx, docx_path)
            if text is not None:
                os.replace(tmp_txt, txt_path)
            else:
                try:
                    txt_path.unlink()
                except FileNotFoundError:
                    pass
            os.replace(tmp_meta, meta_path)
        except OSError as e:
            # 回滚后必须校验实际文件与备份一致；不能沿用发布前的 old_entry 断言
            rolled_back = True
            if had_docx:
                try:
                    os.replace(backup_docx, docx_path)
                except OSError:
                    rolled_back = False
                if rolled_back and backup_docx_sha is not None:
                    rolled_back = (self._sha256_file(docx_path) == backup_docx_sha)
            if had_txt:
                try:
                    os.replace(backup_txt, txt_path)
                except OSError:
                    rolled_back = False
            for p in (tmp_docx, tmp_txt, tmp_meta):
                try:
                    p.unlink()
                except OSError:
                    pass
            if not rolled_back:
                return self._unavailable(
                    module, source, scope,
                    E.issue("upstream_error", True,
                            "落盘失败且回滚未通过校验；备份保留在 %s，请人工恢复（%s）"
                            % (backup_docx, type(e).__name__)),
                    safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
            return _fail(E.issue("upstream_error", True,
                                 "落盘失败，旧版本已回滚并校验（%s）" % type(e).__name__))
        for p in (backup_docx, backup_txt):
            try:
                p.unlink()
            except OSError:
                pass
        text_note = ("txt 为按正文顺序提取的全文" if text_complete
                     else ("全文提取可能不完整（表格解析异常）" if text is not None
                           else "全文提取不可用（python-docx 缺席或文件不可读），docx 原件已落盘"))
        entry = {
            "entry_id": "pyfadoc-%s" % _sha12(acct, name, grade),
            "kind": "training_plan_doc", "plan_name": name,
            "apply_grade": grade,
            "version": meta["version"],
            "total_credit": meta["total_credit"],
            "file_path": str(docx_path), "size_bytes": len(body),
            "content_type": res["content_type"],
            "text_path": meta["text_path"], "text_chars": meta["text_chars"],
            "text_preview": meta["text_preview"],
            "downloaded_at": now_s, "source_id": source,
            "sha256": meta["sha256"], "text_complete": text_complete,
        }
        return self._finalize_jwweb_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=[entry], partial=False, partial_diag=None, total_expected=1,
            empty_evidence=False,
            issues=[E.issue("completeness_unproven", False,
                            "docx 为来源原件字节（已校验 ZIP/DOCX 结构）；%s；"
                            "排版以原文件为准" % text_note)],
            now_s=now_s, done_at=self._now_fn(), limit=1)

    def get_teaching_plans(self, year: str, keyword: str | None = None,
                           cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30 第 4 项：get_teaching_plans。执行计划概览（按年级）。
        year=年级（必填，来源 njdm_id 过滤）；keyword=专业名子串过滤（客户端）。"""
        module, source = MODULE_TEACHING_PLAN, SOURCE_JWGLXT
        kw = (keyword or "").strip().lower()
        scope = "plans-%s:%s" % (year, kw if kw else "ALL")
        safe_url = JWXT_BASE + JXZXJH_PATH + "?gnmkdm=" + JXZXJH_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if not isinstance(year, str) or not re.fullmatch(r"[0-9]{4}", year):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "year 须为 4 位年级（如 2025），用于来源过滤"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_teaching_plans(transport, year)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % fr.diag),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "执行计划读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        rows = fr.items or []
        if kw:
            rows = [c for c in rows if kw in str(c.get("zymc", "")).lower()]
        # R34：映射失败走具名错误路径（有旧成功按契约回退）
        mapped, violations = _map_items(rows, _TEACHING_PLAN_FIELDS,
                                        require_course_name=False)
        if mapped is None:
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", False, "jwglxt 字段不合规"))
            issue = E.issue("upstream_error", False,
                            "来源字段不合规（%d 项）：%s" % (
                                len(violations), "；".join(violations[:6])))
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": self._freshness_jwweb(acct, module, source,
                                                       old, self._now_fn()),
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [issue],
                }
            return self._unavailable(module, source, scope, issue,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        mapped = [m for m in mapped if m.get("major_name")]  # 无专业名行跳过
        import hashlib
        for row in mapped:
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "jwglxt-jxzxjh-" + hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
        # §2.41 R34/R35/R36：统一收尾契约
        return self._finalize_jwweb_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, partial=(fr.outcome == "partial_source"),
            partial_diag=fr.diag, total_expected=fr.total_expected,
            empty_evidence=(fr.outcome == "ok_empty"),
            issues=[E.issue("completeness_unproven", False,
                            "本工具为计划概览；课程明细用 get_teaching_plan_courses（plan_ref）")],
            now_s=now_s, done_at=self._now_fn(), limit=limit)

    def get_teaching_plan_courses(self, plan_ref: str, keyword: str | None = None,
                                  cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30 第 4 项延伸：get_teaching_plan_courses。单执行计划课程明细。
        plan_ref 来自 get_teaching_plans 的 plan_ref（jxzxjhxx_id）。"""
        module, source = MODULE_TEACHING_PLAN_COURSES, SOURCE_JWGLXT
        kw = (keyword or "").strip().lower()
        if not isinstance(plan_ref, str) or not plan_ref.strip():
            return self._unavailable(module, source, "plan",
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "plan_ref 须为执行计划标识（get_teaching_plans 返回的 plan_ref）"),
                                     JWXT_BASE + JXZXJHKC_PATH, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        plan_ref = plan_ref.strip()
        # R37：scope 用完整 plan_ref 与规范化过滤键（不截断、区分无过滤与具体值）
        scope = "plan-courses:%s:%s" % (plan_ref, kw if kw else "ALL")
        safe_url = JWXT_BASE + JXZXJHKC_PATH + "?gnmkdm=" + JXZXJH_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_teaching_plan_courses(transport, plan_ref)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % fr.diag),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "课程明细读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        rows = fr.items or []
        if kw:
            rows = [c for c in rows if kw in str(c.get("kcmc", "")).lower()
                    or kw in str(c.get("kch", "")).lower()]
        # R34：映射失败走具名错误路径（有旧成功按契约回退）
        mapped, violations = _map_items(rows, _TEACHING_PLAN_COURSE_FIELDS)
        if mapped is None:
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", False, "jwglxt 字段不合规"))
            issue = E.issue("upstream_error", False,
                            "来源字段不合规（%d 项）：%s" % (
                                len(violations), "；".join(violations[:6])))
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": self._freshness_jwweb(acct, module, source,
                                                       old, self._now_fn()),
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [issue],
                }
            return self._unavailable(module, source, scope, issue,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        import hashlib
        for row in mapped:
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "jwglxt-kcxx-" + hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
        # §2.41 R34/R35/R36：统一收尾契约
        return self._finalize_jwweb_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, partial=(fr.outcome == "partial_source"),
            partial_diag=fr.diag, total_expected=fr.total_expected,
            empty_evidence=(fr.outcome == "ok_empty"),
            issues=[E.issue("completeness_unproven", False,
                            "课程级列表为来源当前返回；学期分布以 jyxdxnm/jyxdxqm 原文为准")],
            now_s=now_s, done_at=self._now_fn(), limit=limit)

    def get_online_class_urls(self, xnm: str, xqm: str,
                              cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30/B01-06：get_online_class_urls（N1598 网上上课地址）。
        online_url/attachment_path 经 R41 净化（剥离认证类参数与 URI 用户信息）；
        含凭据的链接被净化并在 issues 说明；票据/Cookie 不进输出。"""
        result = self._serve_jwweb(MODULE_ONLINE_URLS, fetch_online_class_urls,
                                   _ONLINE_URL_FIELDS, xnm, xqm, cursor, limit)
        # R41/P1：输出前净化两字段（含缓存返回路径——items 来自快照统一处理）
        issues = list(result.get("issues") or [])
        cred_count = 0
        for item in result.get("items") or []:
            for fld in ("online_url", "attachment_path"):
                raw = item.get(fld)
                if not raw:
                    continue
                clean, had = sanitize_url(raw)
                item[fld] = clean
                if had:
                    cred_count += 1
        if cred_count:
            issues.append(E.issue("credentials_sanitized", False,
                                  "%d 条记录的链接含认证类参数，已净化后输出" % cred_count))
            result["issues"] = issues
        return result

    def get_syllabus(self, xnm: str, xqm: str,
                     cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30/B01-05：get_syllabus（N155002 授课提纲列表，本人本学期）。
        syllabus_id 供正文查询后续接入。"""
        return self._serve_jwweb(MODULE_SYLLABUS, fetch_syllabus_list,
                                 _SYLLABUS_FIELDS, xnm, xqm, cursor, limit)

    def get_innovation_scores(self, cursor: str | None = None, limit: int = 50) -> dict:
        """B01-09：get_innovation_scores（N4780 创新创业获得分值，无学期参数）。
        字段映射为初始通用版（项目/分值/年度/备注），在线样本后精确化。"""
        module, source = MODULE_INNOVATION, SOURCE_JWGLXT
        scope = "innovation"
        safe_url = JWXT_BASE + SZF_PATH + "?gnmkdm=" + SZF_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_innovation_scores(transport)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % fr.diag),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "素质分值读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        # R44：逐字段规范化（str/None 契约；保留合法 0；坏行具名不静默）
        import hashlib
        mapped = []
        bad_rows = 0
        for crow in (fr.items or []):
            if not isinstance(crow, dict):
                bad_rows += 1
                continue

            def _norm(v):
                # 数值 0 合法保留；非 str/None 归一为 str；dict/list 拒绝
                if v is None:
                    return None
                if isinstance(v, (dict, list)):
                    return None  # 类型违约字段
                return str(v)

            # B01-09 真实样本字段（2026-10-15）：xmlbmc/hdfz/sjyxfz/zgfzyq/cj/thfzcz/xh_id
            entry = {
                "project_name": _norm(crow.get("xmlbmc")),
                "score": _norm(crow.get("hdfz") if crow.get("hdfz") is not None
                               else crow.get("zgfz") if crow.get("zgfz") is not None
                               else crow.get("df")),
                "max_require": _norm(crow.get("zgfzyq")),
                "completed": _norm(crow.get("sjyxfz")),
                "grade": _norm(crow.get("cj")),
                "swap_support": _norm(crow.get("thfzcz")),
                "student_id": _norm(crow.get("xh_id")),
            }
            if not any(v is not None for v in entry.values()):
                bad_rows += 1
                continue
            digest = json.dumps(crow, ensure_ascii=False, sort_keys=True)
            entry["entry_id"] = "jwglxt-szf-" + hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            entry["source_id"] = source
            mapped.append(entry)
        issues = [E.issue("completeness_unproven", False,
                          "字段映射按真实样本精确化（2026-10-15：xmlbmc/hdfz/sjyxfz/"
                          "zgfzyq/cj/thfzcz）；未提供报名/申请操作")]
        if bad_rows:
            issues.append(E.issue("upstream_error", False,
                                  "%d 行结构异常/全空已具名跳过" % bad_rows))
        # R44 残余修：映射失败（坏行/对象-only 行）纳入 partial 收尾——
        # 不写 ok 空快照覆盖旧成功、不推进 success、不清健康
        # R44 §2.52：任何映射丢行都纳入 partial（不再要求 mapped 为空）
        partial = (fr.outcome == "partial_source" or bad_rows > 0)
        return self._finalize_jwweb_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, partial=partial,
            partial_diag=(fr.diag or "") + ("；%d 行映射失败" % bad_rows if bad_rows else ""),
            total_expected=fr.total_expected,
            empty_evidence=(fr.outcome == "ok_empty" and not bad_rows),
            issues=issues, now_s=now_s, done_at=self._now_fn(), limit=limit)

    _CLASS_TT_FIELDS = {"kch": "course_code", "kcmc": "course_name",
                        "jxbmc": "teaching_class", "jxb_id": "teaching_class_id",
                        "xm": "teacher", "jgh": "teacher_id",
                        "cdmc": "location", "zcd": "weeks", "jc": "section",
                        "jcor": "section_or", "xqj": "weekday", "xqjmc": "weekday_name",
                        "kcbj": "class_style", "xf": "credit", "kcxz": "class_nature",
                        "kclbmc": "course_category", "xkrs": "enrolled",
                        "xnm": "academic_year", "xqm": "semester"}

    def _finalize_public_success(self, *, acct: str, module: str, source: str,
                                 scope: str, safe_url: str, mapped: list,
                                 outcome: str, diag: str = "", total=None,
                                 bad_rows: int = 0, issues: list | None = None,
                                 now, done_at=None, limit: int = 50,
                                 parser_version: str, evidence_ref: str,
                                 emit_cursor: bool = True) -> dict:
        """§2.76：公共/新增来源统一收尾（与 _finalize_jwweb_success 同契约）。

        R70 扩展：emit_cursor=False 时不发放 cursor（新链不支持续读——
        避免"发出即拒收"的无效游标）。

        outcome ∈ ok / ok_empty / partial_source（失败由调用方先走 unavailable）。
        partial：有旧成功 → 返回旧条目不 upsert；首次 → upsert 带缺口不推进 success。
        """
        issues = list(issues or [])
        done_at = done_at or now
        now_s = now.isoformat(timespec="seconds")
        gap_msg = diag or ("来源声明 %s 条，实取 %d 条" % (total, len(mapped))
                           if (total is not None and len(mapped) < total) else "")
        partial = outcome == "partial_source" or bad_rows > 0
        empty_evidence = outcome == "ok_empty" and not bad_rows
        if partial:
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                self.store.record_issue(
                    acct, module, source, now_s, scope,
                    E.issue("completeness_unproven", True,
                            "本次部分观测（%d 条，来源声明%s条）未入库；保留既有成功快照（%d 条）"
                            % (len(mapped), total, len(old.get("items") or []))))
                issues.append(E.issue("completeness_unproven", False,
                                      "已返回既有成功快照；本次部分观测未入库，缺口见健康记录"))
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                freshness = {
                    "last_attempt_at": now_s, "last_success_at": old_ls,
                    "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                    "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                   "last_success_at": old_ls,
                                   "data_age_seconds": _age_seconds(now, old_ls),
                                   "is_stale": None}],
                }
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": ({"has_more": has_more, "next_cursor": next_cursor,
                                    "result_snapshot_id": old["snapshot_id"]}
                                   if emit_cursor else
                                   {"has_more": False, "next_cursor": None,
                                    "result_snapshot_id": old["snapshot_id"]}),
                    "freshness": freshness,
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     parser_version, evidence_ref),
                    "issues": issues,
                }
        missing = []
        if partial and gap_msg:
            missing = [gap_msg]
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=missing,
            empty_evidence=empty_evidence)
        if partial:
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("completeness_unproven", True,
                                            "首次部分观测：%s；缺口登记" % (gap_msg or "来源未读全")))
        else:
            self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], mapped, 0, limit)
        if not emit_cursor and len(mapped) > limit:
            # B03 r2 反馈⑤：停发游标且有本地截断——必须明示，不能静默压 has_more
            issues.append(E.issue("local_truncated", False,
                                  "本地截断：共 %d 条，limit=%d 仅返回前 %d 条；"
                                  "本来源无续读游标" % (len(mapped), limit, limit)))
        if partial:
            health_row = self._health_row(acct, module, source)
            old_ls = health_row.get("last_success_at") if health_row else None
            freshness = {
                "last_attempt_at": now_s, "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                "by_source": [{"source_id": source, "last_attempt_at": now_s,
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(now, old_ls),
                               "is_stale": None}],
            }
        else:
            freshness = self._freshness(self._health_row(acct, module, source), snap,
                                        now, source)
        return {
            "status": "partial" if partial else "ok",
            "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": ({"has_more": has_more, "next_cursor": next_cursor,
                            "result_snapshot_id": snap["snapshot_id"]}
                           if emit_cursor else
                           {"has_more": False, "next_cursor": None,
                            "result_snapshot_id": snap["snapshot_id"]}),
            "freshness": freshness,
            "source_refs": self._source_refs(snap, source, safe_url,
                                             parser_version, evidence_ref),
            "issues": issues,
        }

    def get_class_timetable(self, bh_id: str | None = None,
                            class_name: str | None = None,
                            xnm: str | None = None,
                            xqm: str | None = None,
                            cursor: str | None = None,
                            limit: int = 100) -> dict:
        """B01-07：get_class_timetable（班级课表，kbcx 模块 bh_id 维度，只读）。
        **长期入口**：bh_id 与 class_name 二选一；xnm/xqm 缺省时同时自动解析
        当前学年和学期（来源 selected；缺失/失效受控失败不猜值）。
        R55：错误范围先建立；partial/坏行走统一收尾。"""
        module, source = MODULE_CLASS_TIMETABLE, SOURCE_JWGLXT
        parser_version, evidence_ref = PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB
        # R55：错误 scope 先建立（后续解析后覆盖）
        scope = "class-tt:pending"
        safe_url = JWXT_BASE + BJKB_PATH + "?gnmkdm=" + BJKB_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, parser_version, evidence_ref)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, parser_version, evidence_ref)
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        # 班级解析（class_name 优先）
        if not bh_id:
            if not class_name:
                return self._unavailable(module, source, scope,
                                         E.issue(E.INVALID_ARGUMENTS, False,
                                                 "bh_id 与 class_name 至少提供一个"),
                                         safe_url, parser_version, evidence_ref)
            rr = resolve_class_bh_id(transport, class_name)
            if rr["outcome"] == "session_expired":
                return self._unavailable(module, source, scope,
                                         E.issue(E.AUTH_REQUIRED, False,
                                                 "教务网页版会话已失效；请重新运行 "
                                                 "python -m src.auth.login_cli init 刷新"),
                                         safe_url, parser_version, evidence_ref)
            if rr["outcome"] != "ok":
                return self._unavailable(module, source, scope,
                                         E.issue("upstream_error", False,
                                                 "班级解析失败（%s：%s）"
                                                 % (rr["outcome"], rr["diag"])),
                                         safe_url, parser_version, evidence_ref)
            bh_id = rr["bh_id"]
        # R55：学期同时解析 selected（不猜 xqm=3；解析失败受控失败）
        if not xnm or not xqm:
            tr = fetch_current_term(transport)
            if tr["outcome"] != "ok":
                return self._unavailable(module, source, scope,
                                         E.issue("upstream_error", False,
                                                 "当前学期解析失败（%s：%s）；请显式传 xnm/xqm"
                                                 % (tr["outcome"], tr["diag"])),
                                         safe_url, parser_version, evidence_ref)
            xnm = xnm or tr["term"]
            xqm = xqm or tr.get("term_xqm")
            # R55 case[8]：selected 缺失受控失败——**不猜值**
            if not xnm or not xqm:
                return self._unavailable(module, source, scope,
                                         E.issue("upstream_error", False,
                                                 "当前学年和/或学期码未解析到（来源下拉无 selected）；"
                                                 "请显式传 xnm/xqm"),
                                         safe_url, parser_version, evidence_ref)
        scope = "class-tt:%s:%s-%s" % (bh_id, xnm, xqm)
        if cursor:
            return self._serve_cursor(acct, module, source, scope, cursor, limit, now,
                                      safe_url, parser_version, evidence_ref, "jwweb")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_class_timetable(transport, bh_id.strip(), xnm, xqm)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s" % fr.diag),
                                     safe_url, parser_version, evidence_ref)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "班级课表读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, parser_version, evidence_ref)
        import hashlib
        mapped = []
        bad_rows = 0
        for row in (fr.items or []):
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            mc, _v = _map_items([row], self._CLASS_TT_FIELDS)
            if mc is None or not mc[0].get("course_name"):
                bad_rows += 1
                continue
            entry = mc[0]
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            entry["entry_id"] = "jwglxt-bjkb-" + hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            entry["source_id"] = source
            mapped.append(entry)
        issues = [E.issue("completeness_unproven", False,
                          "班级课表为来源当前返回（bh_id 维度）；多教师课程为多条记录；"
                          "不自动推断上课状态")]
        if bad_rows:
            issues.append(E.issue("upstream_error", False,
                                  "%d 行结构异常已具名跳过" % bad_rows))
        # R55：统一收尾（partial 保旧/首次缺口/成功时间/续页）
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=fr.outcome, diag=fr.diag, total=fr.total_expected,
            bad_rows=bad_rows, issues=issues, now=now, limit=limit,
            parser_version=parser_version, evidence_ref=evidence_ref)

    def get_free_classrooms(self, dm: str, xqh_id: str = "2", qssj: str = "",
                            jssj: str = "", lh: str = "", cdlb_id: str = "",
                            cdmc: str = "", cursor: str | None = None,
                            limit: int = 50) -> dict:
        """B01-08：get_free_classrooms（N2155 查询空闲教室，只读）。
        dm=学期（如 2026-3，控件值格式）；xqh_id=校区（2=下沙）；日期范围/楼号/
        类别/名称可选。来源行原文输出（raw），字段清单以实际响应为准；
        不提供预约/占座操作。"""
        module, source = MODULE_FREE_CLASSROOMS, SOURCE_JWGLXT
        scope = "free-rooms:%s:%s:%s~%s:%s:%s:%s" % (
            dm, xqh_id, qssj, jssj, lh, cdlb_id, cdmc or "ALL")
        safe_url = JWXT_BASE + KXJS_PATH + "?gnmkdm=" + KXJS_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if not re.fullmatch(r"\d{4}-[0-9]{1,2}", str(dm)):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "dm 须为学期格式（如 2026-3，来源控件值）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_free_classrooms(transport, dm, xqh_id=xqh_id, qssj=qssj,
                                   jssj=jssj, lh=lh, cdlb_id=cdlb_id, cdmc=cdmc)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % fr.diag),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "空闲教室读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        import hashlib
        mapped = []
        bad_rows = 0
        for row in (fr.items or []):
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            mapped.append({
                "entry_id": "jwglxt-kxjs-" + hashlib.sha1(
                    digest.encode("utf-8")).hexdigest()[:16],
                "raw": row, "source_id": source,
            })
        issues = [E.issue("completeness_unproven", False,
                          "来源行原文输出（字段清单以实际响应为准）；不提供预约/占座操作；"
                          "空闲判定以教务系统为准")]
        if bad_rows:
            issues.append(E.issue("upstream_error", False,
                                  "%d 行结构异常已具名跳过" % bad_rows))
        # R50 §2.66：写入前旧成功保护（partial+旧快照 → 返回旧条目不 upsert）；
        # 首次 partial → upsert 带缺口不推进 success
        gap_msg = fr.diag or ("来源未读全或含坏行" if fr.outcome == "partial_source" else "")
        if fr.outcome == "partial_source":
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                self.store.record_issue(
                    acct, module, source, now_s, scope,
                    E.issue("completeness_unproven", True,
                            "本次部分观测（%d 条，来源声明%s条）未入库；保留既有成功快照（%d 条）"
                            % (len(mapped), fr.raw_count, len(old.get("items") or []))))
                issues.append(E.issue("completeness_unproven", False,
                                      "已返回既有成功快照；本次部分观测未入库，缺口见健康记录"))
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                freshness = {
                    "last_attempt_at": now_s, "last_success_at": old_ls,
                    "data_age_seconds": _age_seconds(self._now_fn(), old_ls),
                    "is_stale": None,
                    "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                   "last_success_at": old_ls,
                                   "data_age_seconds": _age_seconds(self._now_fn(), old_ls),
                                   "is_stale": None}],
                }
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": freshness,
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": issues,
                }
            snap = self.store.upsert_snapshot(
                acct, module, source, scope, items=mapped, fetched_at=now_s,
                complete=False, consistency="best_effort",
                missing_ranges=[gap_msg] if gap_msg else [],
                empty_evidence=False)
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("completeness_unproven", True,
                                            "首次部分观测：%s；缺口登记" % (gap_msg or "来源未读全")))
            page, has_more, next_cursor = self._page(acct, module, scope,
                                                     snap["snapshot_id"], mapped, 0, limit)
            health_row = self._health_row(acct, module, source)
            old_ls = health_row.get("last_success_at") if health_row else None
            freshness = {
                "last_attempt_at": now_s, "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(self._now_fn(), old_ls), "is_stale": None,
                "by_source": [{"source_id": source, "last_attempt_at": now_s,
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(self._now_fn(), old_ls),
                               "is_stale": None}],
            }
            return {
                "status": "partial", "module_id": module, "items": page,
                "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
                "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                               "result_snapshot_id": snap["snapshot_id"]},
                "freshness": freshness,
                "source_refs": self._source_refs(snap, source, safe_url,
                                                 PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                "issues": issues,
            }
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=(fr.outcome == "ok_empty"))
        self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], mapped, 0, limit)
        freshness = self._freshness(self._health_row(acct, module, source), snap,
                                    self._now_fn(), source)
        return {
            "status": "ok",
            "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": freshness,
            "source_refs": self._source_refs(snap, source, safe_url,
                                             PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
            "issues": issues,
        }

    def get_retake_courses(self, cxxnm: str, cxxqm: str, keyword: str | None = None,
                           cursor: str | None = None, limit: int = 50) -> dict:
        """重修课程查询（N255720）。cxxnm/cxxqm=重修开设学年学期（如 2026/3）；
        keyword=课程名/代码子串过滤（客户端）。"""
        module, source = MODULE_RETAKE_COURSES, SOURCE_JWGLXT
        kw = (keyword or "").strip().lower()
        scope = "retake-%s-%s:%s" % (cxxnm, cxxqm, kw if kw else "ALL")
        safe_url = JWXT_BASE + CXKCCX_PATH + "?gnmkdm=" + CXKCCX_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        bad_year = (not isinstance(cxxnm, str) or not re.fullmatch(r"[0-9]{4}", cxxnm)
                    or not isinstance(cxxqm, str) or cxxqm not in ("3", "12", "16"))
        if bad_year:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "cxxnm 须为 4 位学年（如 2026），cxxqm 须为学期码 3/12/16"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_retake_courses(transport, cxxnm, cxxqm)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % fr.diag),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "重修课程读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        rows = fr.items or []
        if kw:
            rows = [c for c in rows if kw in str(c.get("kcmc", "")).lower()
                    or kw in str(c.get("kch", "")).lower()]
        # R34：映射失败走具名错误路径（有旧成功按契约回退）
        mapped, violations = _map_items(rows, _RETAKE_FIELDS)
        if mapped is None:
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", False, "jwglxt 字段不合规"))
            issue = E.issue("upstream_error", False,
                            "来源字段不合规（%d 项）：%s" % (
                                len(violations), "；".join(violations[:6])))
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": self._freshness_jwweb(acct, module, source,
                                                       old, self._now_fn()),
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [issue],
                }
            return self._unavailable(module, source, scope, issue,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        import hashlib
        for row in mapped:
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "jwglxt-cxk-" + hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
        # §2.41 R34/R35/R36：统一收尾契约
        return self._finalize_jwweb_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, partial=(fr.outcome == "partial_source"),
            partial_diag=fr.diag, total_expected=fr.total_expected,
            empty_evidence=(fr.outcome == "ok_empty"),
            issues=[E.issue("completeness_unproven", False,
                            "重修报名/选课操作不提供；本工具仅查询开课目录")],
            now_s=now_s, done_at=self._now_fn(), limit=limit)

    _KC_NODE_FIELDS = {"kch": "course_code", "kcmc": "course_name",
                       "jyxdxnmc": "term_year", "jyxdxqmc": "term_code",
                       "xf": "credit", "yxxf": "earned_credit", "xbx": "note"}

    # R42 §2.52：默认安全字段白名单投影（未知字段不默认输出）
    _PROFILE_SAFE = ("学号", "姓名", "性别", "年级", "学院名称", "专业名称", "班级名称",
                     "学籍状态", "学历层次", "培养层次", "学制", "入学日期")
    _PROFILE_SENSITIVE = ("证件号码", "手机号码", "固定电话", "家庭电话", "家庭所在地",
                          "家庭地址", "邮政编码", "出生日期", "籍贯", "户口所在地",
                          "生源地", "考生号", "证件类型", "联系电话", "电子邮箱",
                          "紧急联系人电话")

    def get_my_profile(self, cursor: str | None = None, limit: int = 50,
                       fields: str | None = None,
                       include_sensitive: bool = False) -> dict:
        """§2.30/B01-02：本人基本资料（N100801 渲染页解析）。
        R42：默认仅返回安全字段（学号/姓名/性别/年级/学院/专业/班级/学籍状态等），
        敏感字段（证件/联系方式/住址等）默认不返回；include_sensitive=true 且为
        本人明确查询场景时输出原文。scope 含字段选择。"""
        module, source = MODULE_MY_PROFILE, SOURCE_JWGLXT
        field_key = re.sub(r"[^\w,，]", "", (fields or "")).replace("，", ",")
        scope = "profile:%s:%s" % ("full" if include_sensitive else "safe",
                                   field_key if field_key else "ALL")
        safe_url = JWXT_BASE + GRXX_PATH + "?gnmkdm=" + GRXX_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_my_profile(transport)
        if res["outcome"] == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % res["diag"]),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if res["outcome"] in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "jwglxt %s" % res["outcome"]))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "基本资料读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        import hashlib
        pf = dict(res["profile"])
        issues = []
        # R42 残余：label 归一（变体合并：去尾部冒号/空白）
        norm_map = {}
        for k, v in pf.items():
            nk = re.sub(r"[\s：:]+$", "", str(k)).strip()
            norm_map.setdefault(nk, v)
        pf = norm_map
        if include_sensitive:
            issues.append(E.issue("completeness_unproven", False,
                                  "含敏感字段原文（证件/联系方式/住址）——本人明确请求场景"))
        else:
            # R42 §2.52：默认安全白名单投影（未知字段不默认输出）
            hidden = [k for k in pf if k not in self._PROFILE_SAFE]
            for k in hidden:
                pf.pop(k, None)
            issues.append(E.issue("minimized_output", False,
                                  "已按默认安全投影输出 %d 个字段（省略 %d 个非白名单/敏感字段，"
                                  "含联系方式类）；如需其它字段请在提问中明确说明"
                                  % (len(pf), len(hidden))))
        # R42 残余：fields 选择（敏感过滤后应用）
        if fields:
            want = [w.strip() for w in re.split(r"[,，]", fields) if w.strip()]
            matched = {}
            for w in want:
                for k, v in pf.items():
                    if w in k:
                        matched[k] = v
            pf = matched
            issues.append(E.issue("minimized_output", False,
                                  "已按 fields 选择 %d 个字段" % len(matched)))
        entry = {
            "entry_id": "jwglxt-grxx-" + hashlib.sha1(
                json.dumps(pf, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16],
            "profile": pf, "source_id": source,
        }
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=[entry], fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=False)
        self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], [entry], 0, limit)
        return {
            "status": "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source), snap,
                                         self._now_fn(), source),
            "source_refs": self._source_refs(snap, source, safe_url,
                                             PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
            "issues": issues,
        }

    def get_credit_requirements(self, cursor: str | None = None, limit: int = 50) -> dict:
        """培养方案学分要求节点树（N105505）：各要求节点+节点下课程池
        （含开课学期/学分/已得学分原文）。学号在账号域内部自动获取。"""
        module, source = MODULE_CREDIT_REQUIREMENTS, SOURCE_JWGLXT
        scope = "credit-requirements"
        safe_url = JWXT_BASE + XJYJ_JDLB_PATH + "?gnmkdm=" + XJYJ_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_credit_requirements(transport)
        if res["outcome"] == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % res["diag"]),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if res["outcome"] in ("http_error", "network_error", "parse_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "jwglxt %s" % res["outcome"]))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "学分要求树读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        nodes = res["nodes"] or []
        # 节点展平为条目（node + courses 原文保留）
        # R43 残余修：issues 在循环前初始化（避免 UnboundLocalError）；
        # kcList 按原值检查类型（空 dict 不当合法空）；坏结构→partial 不覆盖旧成功
        import hashlib
        mapped = []
        issues = []
        bad_nodes = 0
        bad_kc_total = 0
        for node in nodes:
            if not isinstance(node, dict):
                bad_nodes += 1
                issues.append(E.issue("upstream_error", False,
                                      "节点结构异常已跳过（非对象）"))
                continue
            node_name = str(node.get("xfyqjdmc") or "").strip()
            kcs_raw = node.get("kcList")
            if kcs_raw is None:
                kcs = []
            elif isinstance(kcs_raw, list):
                kcs = kcs_raw
            else:
                # 空字典/其它类型不当合法空——具名跳过该节点课程
                bad_nodes += 1
                issues.append(E.issue("upstream_error", False,
                                      "节点 %s 的 kcList 类型异常（%s）已跳过"
                                      % (node_name or "?", type(kcs_raw).__name__)))
                kcs = []
            mapped_kcs = []
            bad_kc = 0
            for kc in kcs:
                if not isinstance(kc, dict):
                    bad_kc += 1
                    continue
                # R43 §2.54：kcmc 为课程核心字段——缺失即坏课程（计入 partial）
                mc, _v = _map_items([kc], self._KC_NODE_FIELDS, require_course_name=True)
                if mc is None:
                    bad_kc += 1
                    continue
                mapped_kcs.extend(mc)
            if bad_kc:
                bad_kc_total += bad_kc
                issues.append(E.issue("upstream_error", False,
                                      "节点 %s 有 %d 门课程字段不合规已跳过"
                                      % (node_name or "?", bad_kc)))
            mapped.append({
                "node_name": node_name or None,
                "courses": mapped_kcs,
                "course_count": len(mapped_kcs),
                "entry_id": "jwglxt-xyqjd-" + hashlib.sha1(
                    json.dumps(node, ensure_ascii=False, sort_keys=True,
                               default=str).encode("utf-8")).hexdigest()[:16],
                "source_id": source,
            })
        issues.insert(0, E.issue("completeness_unproven", False,
                                 "节点树为来源当前返回；学分判定以教务系统为准，不推导毕业资格；"
                                 "课程条目字段为白名单原文（yxxf=已得学分）"))
        # R43 §2.52：坏项（bad_nodes/坏课程）→ 受控 partial（保留累计 issues、
        # 不覆盖旧成功、不推进 success）；合法空/正常树 → ok
        partial = bad_nodes > 0 or bad_kc_total > 0
        old = self.store.get_snapshot(acct, module, source, scope)
        if partial and old is not None:
            self.store.record_issue(
                acct, module, source, now_s, scope,
                E.issue("completeness_unproven", True,
                        "本次观测含 %d 个坏结构项/%d 门坏课程未入库；保留既有成功快照"
                        % (bad_nodes, bad_kc_total)))
            issues.append(E.issue("completeness_unproven", False,
                                  "已返回既有成功快照；本次含坏结构项的观测未入库，缺口见健康记录"))
            page, has_more, next_cursor = self._page(
                acct, module, scope, old["snapshot_id"], old.get("items") or [], 0, limit)
            return {
                "status": "partial", "module_id": module, "items": page,
                "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                               "result_snapshot_id": old["snapshot_id"]},
                "freshness": self._freshness_jwweb(acct, module, source,
                                                   old, self._now_fn()),
                "source_refs": self._source_refs(old, source, safe_url,
                                                 PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                "issues": issues,
            }
        # §2.56：missing_ranges 同时含坏节点与坏课程数
        gap_parts = []
        if bad_nodes:
            gap_parts.append("%d 个坏结构项" % bad_nodes)
        if bad_kc_total:
            gap_parts.append("%d 门坏课程" % bad_kc_total)
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort",
            missing_ranges=(["；".join(gap_parts) + "未入库"] if partial else []),
            empty_evidence=(not mapped))
        if partial:
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("completeness_unproven", True,
                                            "含 %s，缺口登记" % "；".join(gap_parts)))
        else:
            self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], mapped, 0, limit)
        if partial:
            # §2.56：首次 partial 无旧成功——last_success_at 必须为 None（不填当前观测）；
            # 有旧成功时分离保留旧时间
            health_row = self._health_row(acct, module, source)
            old_ls = health_row.get("last_success_at") if health_row else None
            freshness = {
                "last_attempt_at": now_s, "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(self._now_fn(), old_ls), "is_stale": None,
                "by_source": [{"source_id": source, "last_attempt_at": now_s,
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(self._now_fn(), old_ls),
                               "is_stale": None}],
            }
        else:
            freshness = self._freshness(self._health_row(acct, module, source), snap,
                                        self._now_fn(), source)
        return {
            "status": "partial" if partial else "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": freshness,   # §2.56：使用构造好的变量（partial 分离/常规各自正确）
            "source_refs": self._source_refs(snap, source, safe_url,
                                             PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
            "issues": issues,
        }

    def get_academic_warnings(self, cursor: str | None = None, limit: int = 50) -> dict:
        """学业预警状态（N305516 服务端渲染页）。无预警→items 含 has_warning=false；
        有预警→summary_text 为页面原文（列表结构以原文为准，不解析推导）。"""
        module, source = MODULE_ACADEMIC_WARNINGS, SOURCE_JWGLXT
        scope = "warnings"
        safe_url = JWXT_BASE + YJCL_PATH + "?gnmkdm=" + YJCL_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_academic_warnings(transport)
        if res["outcome"] == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % res["diag"]),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if res["outcome"] in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "jwglxt %s" % res["outcome"]))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "预警状态读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        import hashlib
        entry = {
            "entry_id": "jwglxt-yjcl-" + hashlib.sha1(
                (res["summary"] or "").encode("utf-8")).hexdigest()[:16],
            "has_warning": not res["no_warning"],
            "summary_text": res["summary"],
            "source_id": source,
        }
        issues = ([E.issue("completeness_unproven", False,
                           "有预警时列表结构以 summary_text 原文为准（未结构化解析）")]
                  if entry["has_warning"] else
                  [E.issue("completeness_unproven", False,
                           "来源页面明示暂无预警；本结果为该次查询的真实状态")])
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=[entry], fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=not entry["has_warning"])
        self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], [entry], 0, limit)
        return {
            "status": "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source), snap,
                                         self._now_fn(), source),
            "source_refs": self._source_refs(snap, source, safe_url,
                                             PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
            "issues": issues,
        }

    def get_academic_progress(self, cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30 第 5 项：get_academic_progress。学业统计（服务端渲染页解析）；
        课程级列表（已修/在修/未修）为前端渲染无独立接口——登记缺口不输出。"""
        module, source = MODULE_ACADEMIC, SOURCE_JWGLXT
        scope = "progress"
        safe_url = JWXT_BASE + XY_PATH + "?gnmkdm=" + XY_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)

        res = fetch_academic_progress(transport)
        if res["outcome"] == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % res["diag"]),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if res["outcome"] in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "jwglxt %s" % res["outcome"]))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "学业统计读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        st = res["stats"]
        # R38：stat_time_text 可空——不直接 replace；entry_id 用内容哈希保持稳定
        import hashlib
        stat_digest = hashlib.sha1(
            json.dumps({k: st.get(k) for k in sorted(st.keys())},
                       ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        entry = {
            "entry_id": "jwglxt-xyqk-" + stat_digest,
            "gpa": st["gpa"], "planned_courses": st["planned_courses"],
            "passed": st["passed"], "failed": st["failed"],
            "not_started": st["not_started"], "in_progress": st["in_progress"],
            "extra_passed": st["extra_passed"], "extra_failed": st["extra_failed"],
            "stat_time_text": st["stat_time_text"], "raw_text": st["raw_text"],
            "source_id": source,
        }
        issues = [E.issue("completeness_unproven", False,
                          "课程级列表（已修/在修/未修）为页面前端渲染、暂未接入——"
                          "本结果仅含学业统计；GPA/门数为来源原文，不推导毕业资格")]
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=[entry], fetched_at=now_s,
            complete=False, consistency="best_effort",
            missing_ranges=["progress 课程级列表待接入"], empty_evidence=False)
        self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], [entry], 0, limit)
        return {
            "status": "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source), snap,
                                         self._now_fn(), source),
            "source_refs": self._source_refs(snap, source, safe_url,
                                             PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
            "issues": issues,
        }

    def get_internship_grades(self, xnm: str, xqm: str,
                              cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30 第 2 项：get_internship_grades。本人各学期实测合法空（接口已验证）；
        items=[] 为真实结果（不推断有无实习经历），以教务页面为准。"""
        return self._serve_jwweb(MODULE_INTERNSHIP, fetch_internship_grades,
                                 _INTERNSHIP_FIELDS, xnm, xqm, cursor, limit)

    # ---------- 工具：成绩明细（两步：课程列表 → 每门项目成绩） ----------

    def get_grade_details(self, xnm: str, xqm: str, course: str | None = None,
                          cursor: str | None = None, limit: int = 50) -> dict:
        """§2.30 第 1 项：get_grade_details。course 为可选过滤（课程名/代码子串，不区分大小写）；
        不过滤时取该学期全部有明细的课程。scope 含课程过滤（不同过滤不共享缓存）。"""
        module, source = MODULE_GRADE_DETAILS, SOURCE_JWGLXT
        course_key = (course or "").strip().lower()
        scope = "%s-%s:detail:%s" % (xnm, xqm, course_key if course_key else "ALL")
        safe_url = JWXT_BASE + GRADE_KC_PATH + "?gnmkdm=" + GRADE_MX_GNM
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False, "limit 须为 1..500 的整数"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        bad = self._validate_xnxq(module, source, scope, xnm, xqm)
        if bad:
            return self._unavailable(module, source, scope, bad,
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if cursor:
            return self._serve_cursor(acct0, module, source, scope, cursor, limit,
                                      self._now_fn(), safe_url, PARSER_VERSION_JWWEB,
                                      EVIDENCE_REF_JWWEB, "jwweb")
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err

        now_s = self._now_fn().isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        issues = []

        # 第一步：课程列表
        fc = fetch_grade_courses(transport, xnm, xqm)
        if fc.outcome == "session_expired":
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue(E.AUTH_REQUIRED, False, "jwglxt 会话失效"))
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s；请重新运行 "
                                             "python -m src.auth.login_cli init 刷新" % fc.diag),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        if fc.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fc.outcome == "network_error",
                                            "jwglxt %s" % fc.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "课程列表读取失败（%s：%s）" % (fc.outcome, fc.diag)),
                                     safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
        # KcList 分页对账：partial_source → 按缺口登记（已取课程仍查明细）
        courses = fc.items or []
        if fc.outcome == "partial_source":
            issues.append(E.issue("completeness_unproven", True,
                                  "课程列表分页不完整：%s" % fc.diag))

        # course 过滤（课程名/代码子串）
        if course_key:
            courses = [c for c in courses
                       if course_key in str(c.get("kcmc", "")).lower()
                       or course_key in str(c.get("kch_id", "")).lower()]
            if not courses:
                # R36：列表 partial 时过滤无匹配≠合法空（来源还有未读课程）——保留缺口
                if fc.outcome == "partial_source":
                    self.store.record_issue(acct, module, source, now_s, scope,
                                            E.issue("completeness_unproven", True,
                                                    "课程列表分页不完整，过滤无匹配不能证明没有"))
                    return self._unavailable(
                        module, source, scope,
                        E.issue("completeness_unproven", True,
                                "来源课程列表分页不完整：%s；过滤「%s」无匹配不能证明没有，"
                                "请待来源完整后再查" % (fc.diag, course)),
                        safe_url, PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB)
                # 完整列表无匹配 → 合法空（真实结果）
                snap = self.store.upsert_snapshot(
                    acct, module, source, scope, items=[], fetched_at=now_s,
                    complete=True, consistency="best_effort", missing_ranges=[],
                    empty_evidence=True)
                self.store.record_success(acct, module, source, now_s, scope)
                return {
                    "status": "ok", "module_id": module, "items": [],
                    "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
                    "pagination": {"has_more": False, "next_cursor": None,
                                   "result_snapshot_id": snap["snapshot_id"]},
                    "freshness": self._freshness(self._health_row(acct, module, source),
                                                 snap, self._now_fn(), source),
                    "source_refs": self._source_refs(snap, source, safe_url,
                                                     PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
                    "issues": [E.issue("completeness_unproven", False,
                                       "来源课程列表无匹配「%s」的课程（该学期有明细课程共%d门，"
                                       "未过滤）" % (course, len(fc.items or [])))],
                }

        # 第二步：逐门项目成绩（单门失败不中断，components_failed=true + 具名 issue）
        entries = []
        comp_failed = 0
        bad_rows = 0
        for crow in courses:
            fc2 = fetch_grade_components(transport, xnm, xqm,
                                         str(crow.get("kch_id") or ""),
                                         str(crow.get("jxb_id") or ""))
            comps, cfailed = [], False
            if fc2.outcome in ("ok", "ok_empty"):
                # R34：组成映射失败具名（components_failed=true），不静默转空
                mapped_c, comp_violations = _map_items(
                    fc2.items or [], _GRADE_COMPONENT_FIELDS, require_course_name=False)
                if mapped_c is None:
                    comps = []
                    cfailed = True
                    issues.append(E.issue("upstream_error", False,
                                          "%s 成绩组成字段不合规：%s" % (
                                              crow.get("kcmc"),
                                              "；".join(comp_violations[:3]))))
                else:
                    comps = mapped_c
                    cfailed = fc2.outcome != "ok"
            else:
                cfailed = True
            if cfailed:
                comp_failed += 1
            mapped, violations = _map_items([crow], _GRADE_COURSE_FIELDS)
            if mapped is None:
                # R34：课程坏行具名登记（partial），不静默丢弃
                bad_rows += 1
                issues.append(E.issue("upstream_error", False,
                                      "课程行字段不合规已跳过：%s（%s）" % (
                                          crow.get("kcmc"),
                                          "；".join(violations[:3]))))
                continue
            entry = mapped[0]
            entry["components"] = comps
            entry["components_failed"] = cfailed
            entry["academic_year"] = xnm
            entry["semester"] = xqm
            digest = json.dumps(crow, ensure_ascii=False, sort_keys=True)
            import hashlib
            entry["entry_id"] = "jwglxt-mx-" + hashlib.sha1(digest.encode("utf-8")).hexdigest()[:16]
            entries.append(entry)
        if comp_failed:
            issues.append(E.issue("upstream_error", True,
                                  "%d 门课程的项目成绩读取失败（components_failed=true 标记）" % comp_failed))
        if fc.outcome != "partial_source" and not any(
                i["code"] == "completeness_unproven" for i in issues):
            issues.append(E.issue("completeness_unproven", False,
                                  "来源未提供整体总数证据：课程列表 totalCount=%s 与实际一致"
                                  % fc.total_expected))

        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=entries, fetched_at=now_s,
            complete=False, consistency="best_effort",
            missing_ranges=(["%s 课程列表不完整" % scope] if fc.outcome == "partial_source" else []),
            empty_evidence=(not entries and not courses))
        self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], entries, 0, limit)
        return {
            "status": "partial" if (fc.outcome == "partial_source" or comp_failed) else "ok",
            "module_id": module,
            "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source), snap,
                                         self._now_fn(), source),
            "source_refs": self._source_refs(snap, source, safe_url,
                                             PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB),
            "issues": issues,
        }


    # ---------- B01-12：教师主页（mypage 公开源，无登录） ----------

    def get_teacher_page(self, keyword: str, detail: bool = False,
                         cursor: str | None = None, limit: int = 50) -> dict:
        """B01-12：get_teacher_page（mypage 公开教师主页门户，无需登录）。
        keyword=姓名/职称/学科等关键词（六字段 or-like，按热度排序）；scope 保留
        关键词内部空白语义（仅 strip 首尾）。acct=ACCOUNT_PUBLIC（公共健康可见）。
        R47：cursor 走快照游标校验（非法/跨查询受控拒绝且不联网）；
        R48：错误对象/坏行≠合法空；partial 走统一收尾契约。"""
        from ..adapters.campus.mypage import search_teachers
        module, source = "teachers.pages", "src.mypage"
        acct = ACCOUNT_PUBLIC
        kw = (keyword or "").strip()          # 仅去首尾（保留内部空白语义）
        scope = "teacher:%s" % (kw if kw else "ALL")
        safe_url = "https://mypage.zjgsu.edu.cn/jscx/list.htm"
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..100 的整数"),
                                     safe_url, "mypage-parser/0.2.0",
                                     "validation/mypage/取证记录.md")
        if not isinstance(keyword, str) or not kw:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "keyword 须为教师姓名/职称/学科等关键词"),
                                     safe_url, "mypage-parser/0.2.0",
                                     "validation/mypage/取证记录.md")
        now = self._now_fn()
        if cursor:
            return self._serve_cursor(acct, module, source, scope, cursor, limit, now,
                                      safe_url, "mypage-parser/0.2.0",
                                      "validation/mypage/取证记录.md", "mypage")
        now_s = now.isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = search_teachers(kw, limit=limit)
        if fr.outcome in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "mypage 读取失败（%s：%s）" % (fr.outcome, fr.diag)))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", fr.outcome == "network_error",
                                             "教师主页读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, "mypage-parser/0.2.0",
                                     "validation/mypage/取证记录.md")
        mapped = []
        for row in (fr.items or []):
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "mypage-teacher-" + hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            row["source_id"] = source
            row.setdefault("home_text", None)   # R60：列表/旧快照路径补正文键
            mapped.append(row)
        issues = []
        if fr.diag:
            for part in fr.diag.split("；"):
                if "净化" in part:
                    issues.append(E.issue("credentials_sanitized", False, part))
                elif "违约" in part:
                    issues.append(E.issue("upstream_error", False, part))
                elif "实取" in part:
                    issues.append(E.issue("completeness_unproven", True, part))
        # B01-12 部分：detail=1 → 附带第一位教师的主页正文（公开页 GET）
        if detail and mapped:
            from ..adapters.campus.mypage import fetch_teacher_home_text
            page_url = mapped[0].get("page_url")
            if page_url:
                fr2 = fetch_teacher_home_text(page_url)
                if fr2.outcome == "ok":
                    mapped[0]["home_text"] = str(fr2.items[0]["text"])[:3000]
                    issues.append(E.issue("completeness_unproven", False,
                                          "主页正文为页面原文片段（3000 字内）；结构化字段后续接入"))
                else:
                    issues.append(E.issue("upstream_error", False,
                                          "主页正文读取失败（%s：%s）" % (fr2.outcome, fr2.diag)))
        issues.append(E.issue("completeness_unproven", False,
                              "公开门户数据（教师本人填写）；按需查询，不做批量导出"))
        # R48 §2.62：写入前执行旧成功保护（partial+旧快照 → 返回旧条目不 upsert）；
        # 首次 partial → upsert 带缺口，不推进 success
        gap_msg = (("来源声明 %s 条，实取 %d 条" % (fr.raw_count, len(mapped)))
                   if (fr.raw_count is not None and len(mapped) < fr.raw_count)
                   else ("来源未读全或含坏行" if fr.outcome == "partial_source" else ""))
        if fr.outcome == "partial_source":
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                self.store.record_issue(
                    acct, module, source, now_s, scope,
                    E.issue("completeness_unproven", True,
                            "本次部分观测（%d 条，来源声明%s条）未入库；保留既有成功快照（%d 条）"
                            % (len(mapped), fr.raw_count, len(old.get("items") or []))))
                issues.append(E.issue("completeness_unproven", False,
                                      "已返回既有成功快照；本次部分观测未入库，缺口见健康记录"))
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"],
                    old.get("items") or [], 0, limit)
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                freshness = {
                    "last_attempt_at": now_s, "last_success_at": old_ls,
                    "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                    "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                   "last_success_at": old_ls,
                                   "data_age_seconds": _age_seconds(now, old_ls),
                                   "is_stale": None}],
                }
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": freshness,
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     "mypage-parser/0.2.0",
                                                     "validation/mypage/取证记录.md"),
                    "issues": issues,
                }
            snap = self.store.upsert_snapshot(
                acct, module, source, scope, items=mapped, fetched_at=now_s,
                complete=False, consistency="best_effort",
                missing_ranges=[gap_msg] if gap_msg else [],
                empty_evidence=False)
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("completeness_unproven", True,
                                            "首次部分观测：%s；缺口登记" % (gap_msg or "来源未读全")))
            page, has_more, next_cursor = self._page(acct, module, scope,
                                                     snap["snapshot_id"], mapped, 0, limit)
            health_row = self._health_row(acct, module, source)
            old_ls = health_row.get("last_success_at") if health_row else None
            freshness = {
                "last_attempt_at": now_s, "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                "by_source": [{"source_id": source, "last_attempt_at": now_s,
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(now, old_ls),
                               "is_stale": None}],
            }
            return {
                "status": "partial", "module_id": module, "items": page,
                "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
                "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                               "result_snapshot_id": snap["snapshot_id"]},
                "freshness": freshness,
                "source_refs": self._source_refs(snap, source, safe_url,
                                                 "mypage-parser/0.2.0",
                                                 "validation/mypage/取证记录.md"),
                "issues": issues,
            }
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=(fr.outcome == "ok_empty"))
        self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], mapped, 0, limit)
        if fr.outcome == "partial_source":
            health_row = self._health_row(acct, module, source)
            old_ls = health_row.get("last_success_at") if health_row else None
            freshness = {
                "last_attempt_at": now_s, "last_success_at": old_ls,
                "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                "by_source": [{"source_id": source, "last_attempt_at": now_s,
                               "last_success_at": old_ls,
                               "data_age_seconds": _age_seconds(now, old_ls),
                               "is_stale": None}],
            }
        else:
            freshness = self._freshness(self._health_row(acct, module, source), snap,
                                        now, source)
        return {
            "status": "partial" if fr.outcome == "partial_source" else "ok",
            "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": freshness,
            "source_refs": self._source_refs(snap, source, safe_url,
                                             "mypage-parser/0.2.0",
                                             "validation/mypage/取证记录.md"),
            "issues": issues,
        }


    # ---------- B01-10：服务与应用目录（App 服务中心） ----------

    def list_campus_services(self, keyword: str | None = None,
                             cursor: str | None = None, limit: int = 50) -> dict:
        """B01-10：list_campus_services（服务中心完整服务目录，公开查询型）。
        keyword=服务名/类别筛选；返回服务名/类别/办理方式/入口 URL/办理量/评分。
        接口取证 2026-10-14（76 服务真实样本）；App token 待 init 后在线验证。"""
        from ..adapters.campus.mypage import fetch_campus_services
        module, source = "services.directory", "src.myapp"
        _sanitize_url = sanitize_url  # R51/R57：统一净化函数
        # §2.78：App 域 transport（会话 app_cookies；无则匿名试读——目录标 auth_required）
        app_cookies = (S.load_session(self.data_dir, S.get_active(self.data_dir) or "") or {}).get(
            "app_cookies") or []
        kw = (keyword or "").strip()
        scope = "services:%s" % (kw if kw else "ALL")
        safe_url = "https://my.zjgsu.edu.cn/"
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 200):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..200 的整数"),
                                     safe_url, "myapp-parser/0.1.0",
                                     "validation/service-center/F09服务台账.md")
        # fwdxid（本科生）从取证样本固定——App 域公开查询参数
        fwdxid = "6E712A8DCDACC79FE050007F010045BA"
        now = self._now_fn()
        if cursor:
            return self._serve_cursor(ACCOUNT_PUBLIC, module, source, scope, cursor,
                                      limit, now, safe_url, "myapp-parser/0.1.0",
                                      "validation/service-center/F09服务台账.md",
                                      "events")
        now_s = now.isoformat(timespec="seconds")
        self.store.record_attempt(ACCOUNT_PUBLIC, module, source, now_s, scope)
        res = fetch_campus_services(None, fwdxid, app_cookies=app_cookies)
        if res["outcome"] in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(ACCOUNT_PUBLIC, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "myapp 读取失败（%s：%s）" % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "服务目录读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, "myapp-parser/0.1.0",
                                     "validation/service-center/F09服务台账.md")
        items_raw = res["items"] or []
        mapped = []
        bad_rows = 0
        for row in items_raw:
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            name = str(row.get("fwmc") or "").strip()
            if not name:
                bad_rows += 1
                continue
            url, had = _sanitize_url(row.get("fwdz"))
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            mapped.append({
                "entry_id": "myapp-svc-" + hashlib.sha1(
                    digest.encode("utf-8")).hexdigest()[:16],
                "service_name": name,
                "category": str(row.get("fwlbmcs") or "").strip() or None,
                "handling_mode": str(row.get("blmsmc") or "").strip() or None,
                "entry_url": url,             # R41 净化
                "handling_count": str(row.get("blcs") or "").strip() or None,
                "rating": str(row.get("fwpf") or "").strip() or None,
                "publish_status": str(row.get("fblxmc") or "").strip() or None,
                "source_id": source,
            })
        if kw:
            matched = [m for m in mapped
                       if kw in (m["service_name"] or "")
                       or kw in (m["category"] or "")]
            issues = []
        else:
            matched = mapped
            issues = []
        issues.append(E.issue("completeness_unproven", False,
                              "服务中心完整目录（2026-10-14 取证 76 服务）；入口 URL 为官方地址；"
                              "App token 在线调用待 init 后验证"))
        # R52 残余 §2.87：bad_rows → 统一收尾（partial 保旧/首次缺口）
        return self._finalize_public_success(
            acct=ACCOUNT_PUBLIC, module=module, source=source, scope=scope,
            safe_url=safe_url, mapped=matched,
            outcome=("partial_source" if bad_rows else "ok"),
            diag=("%d 行结构异常已具名跳过" % bad_rows) if bad_rows else "",
            total=None, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="myapp-parser/0.1.0",
            evidence_ref="validation/service-center/F09服务台账.md")
        snap = self.store.upsert_snapshot(
            ACCOUNT_PUBLIC, module, source, scope, items=matched, fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=(not matched))
        self.store.record_success(ACCOUNT_PUBLIC, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(ACCOUNT_PUBLIC, module, scope,
                                                 snap["snapshot_id"], matched, 0, limit)
        return {
            "status": "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(ACCOUNT_PUBLIC, module, source),
                                         snap, now, source),
            "source_refs": self._source_refs(snap, source, safe_url,
                                             "myapp-parser/0.1.0",
                                             "validation/service-center/F09服务台账.md"),
            "issues": issues,
        }


    # ---------- B01-11：学校官网公开通知（匿名公开源） ----------

    def get_campus_notices(self, detail_url: str | None = None,
                           keyword: str | None = None, page: int = 1,
                           cursor: str | None = None, limit: int = 50) -> dict:
        """B01-11：get_campus_notices（学校官网公开通知列表，匿名 GET，无需登录）。
        返回最新公开通知（标题/URL/日期）。**校级公开通知≠个人收件箱**——
        教务/App 站内消息另列待办，不以此替代。"""
        from ..adapters.campus.mypage import fetch_public_notices
        module, source = "notices.public", "src.news"
        scope = "public-notices:p%d" % max(1, page or 1)
        safe_url = "http://news.zjgsu.edu.cn/18/"
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 200):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..200 的整数"),
                                     safe_url, "news-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        now = self._now_fn()
        # B01-11：detail_url → 通知正文原文
        if detail_url:
            from ..adapters.campus.mypage import fetch_detail_text
            res_d = fetch_detail_text(detail_url,
                                      allowed_hosts=["news.zjgsu.edu.cn"])
            if res_d["outcome"] != "ok":
                return self._unavailable(module, source, "notice-detail",
                                         E.issue("upstream_error", False,
                                                 "通知正文读取失败（%s：%s）"
                                                 % (res_d["outcome"], res_d["diag"])),
                                         safe_url, "news-parser/0.1.0",
                                         "validation/mypage/取证记录.md")
            return {
                "status": "ok", "module_id": module,
                "items": [{"detail_url": detail_url, "detail_text": res_d["text"],
                           "source_id": source}],
                "coverage": {"source_snapshots": []},
                "pagination": {"has_more": False, "next_cursor": None,
                               "result_snapshot_id": None},
                "freshness": {"last_attempt_at": now.isoformat(timespec="seconds"),
                              "last_success_at": now.isoformat(timespec="seconds"),
                              "data_age_seconds": 0, "is_stale": None,
                              "by_source": []},
                "source_refs": [],
                "issues": [E.issue("completeness_unproven", False,
                                   "通知正文原文（3000 字内）")],
            }
        if cursor:
            return self._serve_cursor(ACCOUNT_PUBLIC, module, source, scope, cursor,
                                      limit, now, safe_url, "news-parser/0.1.0",
                                      "validation/mypage/取证记录.md", "events")
        now_s = now.isoformat(timespec="seconds")
        self.store.record_attempt(ACCOUNT_PUBLIC, module, source, now_s, scope)
        res = fetch_public_notices()
        if res["outcome"] in ("network_error", "http_error", "parse_error"):
            self.store.record_issue(ACCOUNT_PUBLIC, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "通知列表读取失败（%s：%s）" % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "通知读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, "news-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        bad_rows = 0
        kw = (keyword or "").strip()
        for row in (res["items"] or []):
            if not isinstance(row, dict) or not (row.get("title") or "").strip():
                bad_rows += 1
                continue
            if kw and kw not in (row.get("title") or ""):
                continue   # keyword 本地筛选
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            mapped.append({
                "entry_id": "news-notice-" + _hashlib.sha1(
                    digest.encode("utf-8")).hexdigest()[:16],
                "title": row.get("title"),
                "url": row.get("url"),
                "date": row.get("date"),
                "detail_text": None,   # R60：列表路径补正文键
                "source_id": source,
            })
        issues = [E.issue("completeness_unproven", False,
                          "校级公开通知（新闻网，匿名可读）；**不代表教务/App 个人收件箱**——"
                          "个人站内消息另列待办；列表为来源当前首页条目")]
        # §2.87 R54 残余：通知收尾接统一 helper（坏行 partial/保旧）
        return self._finalize_public_success(
            acct=ACCOUNT_PUBLIC, module=module, source=source, scope=scope,
            safe_url=safe_url, mapped=mapped,
            outcome=(res["outcome"] if bad_rows == 0 else "partial_source"),
            diag=("%d 行结构异常已具名跳过" % bad_rows) if bad_rows else "",
            total=None, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="news-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md")
        snap = self.store.upsert_snapshot(
            ACCOUNT_PUBLIC, module, source, scope, items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=not mapped)
        self.store.record_success(ACCOUNT_PUBLIC, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(ACCOUNT_PUBLIC, module, scope,
                                                 snap["snapshot_id"], mapped, 0, limit)
        return {
            "status": "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(ACCOUNT_PUBLIC, module, source),
                                         snap, now, source),
            "source_refs": self._source_refs(snap, source, safe_url,
                                             "news-parser/0.1.0",
                                             "validation/mypage/取证记录.md"),
            "issues": issues,
        }


    # ---------- B01 招聘会（jyw 智慧就业平台，匿名公开源） ----------

    def get_job_fairs(self, detail_id: str | None = None,
                      cursor: str | None = None, limit: int = 50) -> dict:
        """B01：get_job_fairs（招聘会场次列表，jyw 平台匿名公开源，无需登录）。
        返回 113 场全量（含已结束）。**举办状态以详情页邀请函正文为准，不自动推断
        进行中/已结束**（列表字段 jbjssj ≠ 实际线下结束——项目使用者 2026-10-15 指出）。"""
        from ..adapters.campus.mypage import fetch_job_fairs
        module, source = "campus.job_fairs", "src.jyw-zph"
        scope = "job-fairs"
        safe_url = "https://jyw.zjgsu.edu.cn/career/zphxx"
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 200):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..200 的整数"),
                                     safe_url, "jyw-parser/0.1.0",
                                     "validation/career/取证记录.md")
        now = self._now_fn()
        # B01：detail_id（zphid）→ 招聘会详情邀请函正文（状态判定以正文为准的落地）
        if detail_id:
            from ..adapters.campus.mypage import fetch_detail_text
            d_url = ("https://jyw.zjgsu.edu.cn/career/zphxx/view/" + detail_id)
            res_d = fetch_detail_text(d_url, allowed_hosts=["jyw.zjgsu.edu.cn"])
            if res_d["outcome"] != "ok":
                return self._unavailable(module, source, "job-fair-detail",
                                         E.issue("upstream_error", False,
                                                 "详情正文读取失败（%s：%s）"
                                                 % (res_d["outcome"], res_d["diag"])),
                                         d_url, "jyw-parser/0.1.0",
                                         "validation/career/取证记录.md")
            return {
                "status": "ok", "module_id": module,
                "items": [{"detail_id": detail_id, "detail_text": res_d["text"],
                           "detail_url": d_url, "source_id": source}],
                "coverage": {"source_snapshots": []},
                "pagination": {"has_more": False, "next_cursor": None,
                               "result_snapshot_id": None},
                "freshness": {"last_attempt_at": now.isoformat(timespec="seconds"),
                              "last_success_at": now.isoformat(timespec="seconds"),
                              "data_age_seconds": 0, "is_stale": None,
                              "by_source": []},
                "source_refs": [],
                "issues": [E.issue("completeness_unproven", False,
                                   "邀请函正文原文（3000 字内）；**举办/报名状态以此正文描述为准**")],
            }
        if cursor:
            return self._serve_cursor(ACCOUNT_PUBLIC, module, source, scope, cursor,
                                      limit, now, safe_url, "jyw-parser/0.1.0",
                                      "validation/career/取证记录.md", "events")
        now_s = now.isoformat(timespec="seconds")
        self.store.record_attempt(ACCOUNT_PUBLIC, module, source, now_s, scope)
        res = fetch_job_fairs()
        if res["outcome"] in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(ACCOUNT_PUBLIC, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "招聘会列表读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "招聘会读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, "jyw-parser/0.1.0",
                                     "validation/career/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        bad_rows = 0
        for row in (res["items"] or []):
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "jyw-zph-" + _hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            row["source_id"] = source
            mapped.append(row)
        issues = []
        if res["diag"]:
            issues.append(E.issue("upstream_error", False, res["diag"]))
        issues.append(E.issue("completeness_unproven", False,
                              "全量 %s 场；**举办状态以详情页邀请函正文为准，不自动推断"
                              "进行中/已结束**（列表 jbjssj ≠ 实际线下结束）" % res["total"]))
        # R56 §2.76：统一收尾（partial 保旧/首次缺口/成功时间/续页）
        return self._finalize_public_success(
            acct=ACCOUNT_PUBLIC, module=module, source=source, scope=scope,
            safe_url=safe_url, mapped=mapped, outcome=res["outcome"], diag=res["diag"],
            total=res["total"], bad_rows=bad_rows, issues=issues, now=now, limit=limit,
            parser_version="jyw-parser/0.1.0",
            evidence_ref="validation/career/取证记录.md")

    _CAREER_CATEGORIES = {
        "zpxx": "全职招聘", "sxzpxx": "实习招聘", "zpgg": "招聘公告",
        "zygh": "职业规划", "xyfc": "学院风采", "zprd": "招聘热点",
        "zcty": "职场体验", "syxx": "生源速览", "lxzpzn": "招聘指南",
        "zyghds": "职业生涯规划大赛",
    }

    def get_career_info(self, category: str, keyword: str | None = None,
                        page: int = 1, cursor: str | None = None,
                        limit: int = 20) -> dict:
        """B01-F32：get_career_info（智慧就业网通用栏目查询，匿名公开源）。
        category ∈ 全职招聘/实习招聘/招聘公告/职业规划/学院风采/招聘热点/职场体验/
        生源速览/招聘指南/大赛（代码或中文均可）；keyword 可选筛选；page 为来源页码。
        全职招聘 15480 条/实习招聘 3093 条/公告 78 条等（2026-10-15 全栏目实测）。
        职位投递/简历等写操作不提供。"""
        from ..adapters.campus.mypage import fetch_career_info, CAREER_CATEGORIES, _sanitize_url
        module, source = "career.info", "src.jyw-career"
        cat = (category or "").strip()
        # 中文标签 → 代码
        code_map = {v: k for k, v in self._CAREER_CATEGORIES.items()}
        cat = code_map.get(cat, cat)
        kw = (keyword or "").strip()
        scope = "career:%s:%s:p%s" % (cat, kw or "ALL", page)
        safe_url = "https://jyw.zjgsu.edu.cn/career/index"
        if cat not in self._CAREER_CATEGORIES:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "category 须为以下之一：%s（代码或中文均可）"
                                             % "、".join(self._CAREER_CATEGORIES.values())),
                                     safe_url, "jyw-parser/0.1.0",
                                     "validation/career/取证记录.md")
        if isinstance(page, bool) or not isinstance(page, int) or not (1 <= page <= 2000):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "page 须为 1..2000 的整数"),
                                     safe_url, "jyw-parser/0.1.0",
                                     "validation/career/取证记录.md")
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..100 的整数"),
                                     safe_url, "jyw-parser/0.1.0",
                                     "validation/career/取证记录.md")
        label = self._CAREER_CATEGORIES[cat]
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        if cursor:
            return self._serve_cursor(acct, module, source, scope, cursor, limit, now,
                                      safe_url, "jyw-parser/0.1.0",
                                      "validation/career/取证记录.md", "events")
        now_s = now.isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_career_info(cat, keyword=kw, page=page)
        if res["outcome"] in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "jyw 读取失败（%s：%s）" % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "%s 读取失败（%s：%s）" % (label, res["outcome"], res["diag"])),
                                     safe_url, "jyw-parser/0.1.0",
                                     "validation/career/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        bad_rows = 0
        cred_rows = 0
        for row in (res["items"] or []):
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            # zpxx 系（岗位）与 news 系（文章）字段不同——通用提取
            # news 系真实字段（2026-10-15 实测）：newsTitle/newsUrl/releaseDate/newsAttach
            if cat in ("zpxx", "sxzpxx"):
                title = str(row.get("zpzt") or "").strip()
                url, had = _sanitize_url(row.get("zpxxwz"))
                extra = {"deadline": str(row.get("zpjzrq") or "").strip() or None,
                         "demand": str(row.get("xqrsmc") or "").strip() or None,
                         "unit": str(row.get("dwmc") or row.get("orgName") or "").strip() or None,
                         "address": str(row.get("xxdz") or "").strip() or None,
                         "job_id": str(row.get("zpxxid") or "").strip() or None}
            else:
                title = str(row.get("newsTitle") or "").strip()
                url, had = _sanitize_url(row.get("newsUrl")) if row.get("newsUrl") else (None, False)
                # R57 残余：newsAttach 原始 JSON 含附件 URL（可能带认证参数）——逐个净化
                attach_raw = row.get("newsAttach")
                attach_clean = None
                if attach_raw:
                    try:
                        atts = json.loads(attach_raw)
                        if isinstance(atts, list):
                            for a in atts:
                                if isinstance(a, dict) and a.get("attachUrl"):
                                    au, h2 = _sanitize_url(str(a["attachUrl"]))
                                    a["attachUrl"] = au
                                    if h2:
                                        cred_rows += 1
                        attach_clean = json.dumps(atts, ensure_ascii=False)
                    except Exception:
                        attach_clean = None  # 解析失败不放原始串（可能含认证参数）
                extra = {"date": str(row.get("releaseDate") or "").strip() or None,
                         "attach": attach_clean}
            if not title:
                bad_rows += 1
                continue
            if had:
                cred_rows += 1
            # R57/P1：只投影安全字段——原始 row（含 zpxxwz 等原始 URL）不进输出/缓存
            proj = {
                "entry_id": "jyw-career-" + _hashlib.sha1(
                    json.dumps({"t": title, "u": url, "c": cat, "p": page},
                               ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16],
                "category_label": label,
                "title": title,
                "entry_url": url,
                "source_id": source,
            }
            proj.update(extra)
            mapped.append(proj)
        issues = []
        if bad_rows:
            issues.append(E.issue("upstream_error", False,
                                  "%d 行结构异常已具名跳过" % bad_rows))
        if cred_rows:
            issues.append(E.issue("credentials_sanitized", False,
                                  "%d 条链接含认证类参数已净化" % cred_rows))
        issues.append(E.issue("completeness_unproven", False,
                              "%s（来源声明 %s 条，本页 %d 条）；岗位投递/简历等写操作不提供"
                              % (label, res["total"], len(mapped))))
        # R57 §2.76：统一收尾（partial 保旧/首次缺口）
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res["diag"],
            total=int(res["total"]) if res["total"] else None,
            bad_rows=bad_rows, issues=issues, now=now, limit=limit,
            parser_version="jyw-parser/0.1.0",
            evidence_ref="validation/career/取证记录.md")
        # （以下为原收尾——已被 helper 替代，保留占位避免误删后续方法）
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort",
            missing_ranges=(["来源声明 %s 条" % res["total"]]
                            if (res["total"] and len(mapped) < int(res["total"])) else []),
            empty_evidence=(not mapped and res["outcome"] == "ok"))
        self.store.record_success(acct, module, source, now_s, scope)
        page_items, has_more, next_cursor = self._page(acct, module, scope,
                                                       snap["snapshot_id"], mapped,
                                                       0, limit)
        return {
            "status": "ok", "module_id": module, "items": page_items,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source),
                                         snap, now, source),
            "source_refs": self._source_refs(snap, source, safe_url,
                                             "jyw-parser/0.1.0",
                                             "validation/career/取证记录.md"),
            "issues": issues,
        }


    # ---------- B01-F25/F07：学校新闻网 + 图书馆公开信息 ----------

    _NEWS_CATEGORIES = {"2": "商大新闻", "3": "综合新闻", "17": "学术研究",
                        "5": "媒体商大", "18": "通知公告", "4": "教学科研",
                        "7": "理论学院", "24": "专题列表"}

    def get_campus_news(self, category: str = "3", detail_url: str | None = None,
                        keyword: str | None = None, page: int = 1,
                        cursor: str | None = None, limit: int = 30) -> dict:
        """F07：get_campus_news（学校新闻网栏目列表/详情正文，匿名公开源）。
        category ∈ 商大新闻/综合新闻/学术研究/媒体商大/通知公告（代码 2/3/17/5/18）；
        detail_url=新闻网详情链接时返回正文原文（3000 字内）。"""
        from ..adapters.campus.mypage import (fetch_news_category,
                                              fetch_news_detail, NEWS_CATEGORIES)
        module, source = "news.campus", "src.news"
        cat = (category or "").strip()
        code_map = {v: k for k, v in self._NEWS_CATEGORIES.items()}
        cat = code_map.get(cat, cat)
        label = NEWS_CATEGORIES.get(cat)
        if not label:
            return self._unavailable(module, source, "news:%s" % cat,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "category 须为：%s（代码或名称均可）"
                                             % "、".join(NEWS_CATEGORIES.values())),
                                     "http://news.zjgsu.edu.cn/", "news-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, "news:" + cat)
        if detail_url:
            res_d = fetch_news_detail(detail_url)
            if res_d["outcome"] != "ok":
                return self._unavailable(module, source, "news-detail",
                                         E.issue("upstream_error", False,
                                                 "新闻正文读取失败（%s：%s）"
                                                 % (res_d["outcome"], res_d["diag"])),
                                         detail_url, "news-parser/0.1.0",
                                         "validation/mypage/取证记录.md")
            return {
                "status": "ok", "module_id": module,
                "items": [{"entry_id": "campus-news-detail", "title": None,
                           "url": detail_url, "date": None,
                           "detail_text": res_d["text"], "source_id": source}],
                "coverage": {"source_snapshots": []},
                "pagination": {"has_more": False, "next_cursor": None,
                               "result_snapshot_id": None},
                "freshness": {"last_attempt_at": now_s,
                              "last_success_at": now_s, "data_age_seconds": 0,
                              "is_stale": None, "by_source": []},
                "source_refs": [],
                "issues": [E.issue("completeness_unproven", False,
                                   "新闻正文原文（3000 字内）")],
            }
        res = fetch_news_category(cat)
        if res["outcome"] in ("network_error", "http_error", "parse_error"):
            self.store.record_issue(acct, module, source, now_s, "news:" + cat,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "新闻网读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, "news:" + cat,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "新闻读取失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     "http://news.zjgsu.edu.cn/", "news-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        kw = (keyword or "").strip()
        for row in (res["items"] or []):
            if kw and kw not in (row.get("title") or ""):
                continue   # keyword 本地筛选（标题包含）
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "campus-news-" + _hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            row["source_id"] = source
            row["category_label"] = label
            row["detail_text"] = None
            mapped.append(row)
        issues = [E.issue("completeness_unproven", False,
                          "%s 第 %d 页条目（匿名公开源）；detail_url 传新闻网链接可取正文" % (label, page))]
        if cursor:
            # R62：不支持分页续读——**cursor 时不重抓源**，受控失败说明
            return self._unavailable(module, source, "news:" + cat,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "新闻网列表暂不支持分页续读（cursor 不消费）"
                                             "——请不带 cursor 重新查询"),
                                     "http://news.zjgsu.edu.cn/", "news-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        snap = self.store.upsert_snapshot(
            acct, module, source, "news:%s:p%d" % (cat, max(1, page or 1)),
            items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=not mapped)
        self.store.record_success(acct, module, source, now_s, "news:" + cat)
        page, has_more, next_cursor = self._page(acct, module, "news:" + cat,
                                                 snap["snapshot_id"], mapped, 0, limit)
        has_more, next_cursor = False, None   # R62：不承诺分页
        return {
            "status": "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block("news:" + cat, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source),
                                         snap, now, source),
            "source_refs": self._source_refs(snap, source, "http://news.zjgsu.edu.cn/",
                                             "news-parser/0.1.0",
                                             "validation/mypage/取证记录.md"),
            "issues": issues,
        }

    def get_library_info(self, category: str = "gzbj",
                         cursor: str | None = None, limit: int = 30) -> dict:
        """F25/F21：get_library_info（图书馆公开信息：馆藏布局/规章制度/常见问题/借阅服务）。
        category ∈ gzbj/馆藏布局、fgzd/规章制度、cjwt/常见问题、jyfw/借阅服务。
        馆藏布局返回分布表原文；栏目类返回条目列表。匿名公开源。"""
        from ..adapters.campus.mypage import fetch_library_info, LIB_CATEGORIES
        module, source = "library.info", "src.library"
        cat = (category or "").strip()
        label = LIB_CATEGORIES.get(cat, (None, None))[0] if cat in LIB_CATEGORIES else None
        if not label:
            # 中文→代码
            code_map = {v[0]: k for k, v in LIB_CATEGORIES.items()}
            cat = code_map.get(cat, cat)
            label = LIB_CATEGORIES.get(cat, (None,))[0]
        if not label:
            return self._unavailable(module, source, "library:" + cat,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "category 须为：%s"
                                             % "、".join(v[0] for v in LIB_CATEGORIES.values())),
                                     "http://lib.zjgsu.edu.cn/", "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        scope = "library:" + cat
        if cursor:
            # R62：不支持分页续读——cursor 时不重抓源
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "图书馆栏目暂不支持分页续读（cursor 不消费）"
                                             "——请不带 cursor 重新查询"),
                                     "http://lib.zjgsu.edu.cn/", "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_library_info(cat)
        if res["outcome"] in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "图书馆读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "图书馆信息读取失败（%s：%s）"
                                             % (res["outcome"], res["diag"])),
                                     "http://lib.zjgsu.edu.cn/", "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        for row in (res["items"] or []):
            # R61：统一投影 LibraryInfoEntry 全键（adapter 各 shape 补 None）
            proj = {
                "entry_id": "lib-" + _hashlib.sha1(
                    json.dumps(row, ensure_ascii=False, sort_keys=True,
                               default=str).encode("utf-8")).hexdigest()[:16],
                "title": (str(row.get("title")) if row.get("title") is not None
                          else None),
                "url": row.get("url"),
                "date": row.get("date"),
                "text": row.get("text"),
                "category_label": label,
                "source_id": source,
            }
            mapped.append(proj)
        issues = [E.issue("completeness_unproven", False,
                          "%s（图书馆公开页面原文，匿名可读）" % label)]
        snap = self.store.upsert_snapshot(
            acct, module, source, scope, items=mapped, fetched_at=now_s,
            complete=False, consistency="best_effort", missing_ranges=[],
            empty_evidence=not mapped)
        self.store.record_success(acct, module, source, now_s, scope)
        page, has_more, next_cursor = self._page(acct, module, scope,
                                                 snap["snapshot_id"], mapped, 0, limit)
        has_more, next_cursor = False, None   # R62：不承诺分页
        if cursor:
            issues.append(E.issue("completeness_unproven", False,
                                  "图书馆栏目暂不支持分页续读（返回当前页）"))
        return {
            "status": "ok", "module_id": module, "items": page,
            "coverage": {"source_snapshots": [self._snap_block(scope, snap, source)]},
            "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                           "result_snapshot_id": snap["snapshot_id"]},
            "freshness": self._freshness(self._health_row(acct, module, source),
                                         snap, now, source),
            "source_refs": self._source_refs(snap, source, "http://lib.zjgsu.edu.cn/",
                                             "lib-parser/0.1.0",
                                             "validation/mypage/取证记录.md"),
            "issues": issues,
        }


    # ---------- F05/F11：考级项目报名状态 ----------

    def get_exam_registrations(self, cursor: str | None = None,
                               limit: int = 30) -> dict:
        """F05/F11：get_exam_registrations（考级项目报名状态，教务网页版本人视角）。
        空列表=当前无报名记录（真实空状态，非缺口）。报名/缴费属写操作不提供。"""
        from ..adapters.campus.jwweb import fetch_exam_registrations
        module, source = MODULE_EXAM_REGISTRATIONS, SOURCE_JWGLXT
        parser_version, evidence_ref = PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB
        scope = "exam-registrations"
        safe_url = JWXT_BASE + "/jwglxt/kjgl/kjbm_cxXskjbm.html?gnmkdm=N2510"
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, parser_version, evidence_ref)
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, scope)
        if err is not None:
            return err
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._serve_cursor(acct, module, source, scope, cursor, limit,
                                      now, safe_url, parser_version, evidence_ref,
                                      "jwweb")
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_exam_registrations(transport)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s" % fr.diag),
                                     safe_url, parser_version, evidence_ref)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "考级报名状态读取失败（%s：%s）"
                                             % (fr.outcome, fr.diag)),
                                     safe_url, parser_version, evidence_ref)
        import hashlib
        mapped = []
        bad_rows = 0
        for row in (fr.items or []):
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            mc, _v = _map_items([row], {
                "xmmc": "project_name", "xm": "project_name",
                "bmztmc": "status", "bmzt": "status_code",
                "bmsj": "signup_time", "kssj": "exam_date",
            }, require_course_name=False)
            if mc is None:
                bad_rows += 1
                continue
            entry = mc[0]
            entry["entry_id"] = "jwglxt-kjbm-" + hashlib.sha1(
                json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            entry["source_id"] = source
            mapped.append(entry)
        issues = [E.issue("completeness_unproven", False,
                          "考级报名状态为学生端本人视角；空列表=当前无报名记录（真实空状态）；"
                          "报名/缴费属写操作不提供")]
        if bad_rows:
            issues.append(E.issue("upstream_error", False,
                                  "%d 行结构异常已具名跳过" % bad_rows))
        # 统一收尾（partial 保旧/首次缺口）——R64：未知行→partial（全 None 行不 ok）
        _out = ("partial_source" if (bad_rows and fr.outcome == "ok")
                else fr.outcome)
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=_out,
            diag=(fr.diag + ("；%d 行结构异常已具名跳过" % bad_rows) if bad_rows
                  else fr.diag),
            total=fr.total_expected, bad_rows=bad_rows, issues=issues, now=now,
            limit=limit, parser_version=parser_version, evidence_ref=evidence_ref)


    # ---------- F08：门户消息/通知（App 域，本人视角） ----------

    def get_my_messages(self, cursor: str | None = None, limit: int = 20) -> dict:
        """F08：get_my_messages（师生之家门户消息/通知，本人视角，App 域会话）。
        空列表/来源接口异常如实返回（不推断有无未读）。仅只读。"""
        from ..adapters.campus.mypage import fetch_app_messages
        module, source = "app.messages", "src.myapp"
        scope = "my-messages"
        safe_url = "https://my.zjgsu.edu.cn/"
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     safe_url, "myapp-parser/0.1.0",
                                     "validation/service-center/取证记录.md")
        sess_info = (S.load_session(self.data_dir, S.get_active(self.data_dir) or "")
                     or {})
        app_cookies = sess_info.get("app_cookies") or []
        if not app_cookies:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "App 域会话未建立：请重跑 login_cli init"),
                                     safe_url, "myapp-parser/0.1.0",
                                     "validation/service-center/取证记录.md")
        acct = acct0   # R59：本人消息按账号域缓存（非 PUBLIC）
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._serve_cursor(acct, module, source, scope, cursor, limit,
                                      now, safe_url, "myapp-parser/0.1.0",
                                      "validation/service-center/取证记录.md", "events")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_app_messages(app_cookies)
        if res["outcome"] in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "门户消息读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "门户消息读取失败（%s：%s）"
                                             % (res["outcome"], res["diag"])),
                                     safe_url, "myapp-parser/0.1.0",
                                     "validation/service-center/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        for row in (res["items"] or []):
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "myapp-msg-" + _hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            row["source_id"] = source
            mapped.append(row)
        issues = [E.issue("completeness_unproven", False,
                          "门户消息/通知（本人视角）；空列表=当前无消息或来源接口异常/空；"
                          "已读状态不推断")]
        if res["diag"]:
            issues.append(E.issue("upstream_error", False, res["diag"]))
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=None, bad_rows=res.get("bad_rows") or 0, issues=issues, now=now,
            limit=limit, parser_version="myapp-parser/0.1.0",
            evidence_ref="validation/service-center/取证记录.md")


    # ---------- B01-03：全校课程信息查询（N151066 viewFunc） ----------

    _COURSE_CATALOG_FIELDS = {
        "kch": "course_code", "kcmc": "course_name", "kcywmc": "course_name_en",
        "xf": "credit", "zxs": "total_hours", "xsxxxx": "hours_detail",
        "kclbmc": "course_category", "kkbmmc": "school",
        "kch_id": "course_key",
    }

    @staticmethod
    def _str_or_none(v):
        """R66：数字/其它类型归一为字符串（模型 str 契约）。"""
        if v is None:
            return None
        return str(v)

    def get_course_catalog(self, xnm: str | None = None, xqm: str | None = None,
                           keyword: str | None = None, page: int = 1,
                           cursor: str | None = None, limit: int = 20) -> dict:
        """B01-03：get_course_catalog（全校课程信息查询，教务网页版本人视角）。
        keyword=课程名本地筛选（来源不支持服务端筛选，Agent 可翻页组合）；
        返回课程号/名称/英文名/学分/学时/类别/开课学院。"""
        from ..adapters.campus.jwweb import fetch_course_catalog
        module, source = MODULE_COURSE_CATALOG, SOURCE_JWGLXT
        parser_version, evidence_ref = PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, "course-catalog",
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     "https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                                     "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066",
                                     parser_version, evidence_ref)
        acct, _cookies, transport, err = self._jwweb_ctx(
            module, source, "course-catalog")
        if err is not None:
            return err
        safe_url = ("https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                    "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066")
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        # R66：cursor 消费——scope 传 ""（用 payload 里的首屏 scope）
        if cursor:
            return self._serve_cursor(acct, module, source, "",
                                      cursor, limit, now, safe_url,
                                      parser_version, evidence_ref, "jwweb")
        # R59/R55 同款：学期缺省自动解析（不猜值）
        if not xnm or not xqm:
            tr = fetch_current_term(transport)
            if tr["outcome"] != "ok":
                return self._unavailable(module, source, "course-catalog",
                                         E.issue("upstream_error", False,
                                                 "当前学期解析失败（%s：%s）；请显式传 xnm/xqm"
                                                 % (tr["outcome"], tr["diag"])),
                                         "https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                                         "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066",
                                         parser_version, evidence_ref)
            xnm = xnm or tr["term"]
            xqm = xqm or tr.get("term_xqm")
            if not xnm or not xqm:
                return self._unavailable(module, source, "course-catalog",
                                         E.issue("upstream_error", False,
                                                 "当前学年和/或学期码未解析到；请显式传 xnm/xqm"),
                                         "https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                                         "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066",
                                         parser_version, evidence_ref)
        if isinstance(page, bool) or not isinstance(page, int) or not (1 <= page <= 1200):
            return self._unavailable(module, source, "course-catalog",
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "page 须为 1..1200 的整数（每页 15 条）"),
                                     "https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                                     "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066",
                                     parser_version, evidence_ref)
        kw2 = (keyword or "").strip()
        # R66：keyword 入 scope（不同筛选不共用缓存）
        scope = ("course-catalog:%s-%s:p%d" % (xnm, xqm, page)
                 + (":kw:%s" % kw2 if kw2 else ""))
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_course_catalog(transport, xnm, xqm, page=page)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s" % fr.diag),
                                     "https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                                     "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066",
                                     parser_version, evidence_ref)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "课程信息读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     "https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                                     "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066",
                                     parser_version, evidence_ref)
        import hashlib as _hashlib
        mapped = []
        bad_rows = 0
        kw = (keyword or "").strip()
        for row in (fr.items or []):
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            mc, _v = _map_items([row], self._COURSE_CATALOG_FIELDS)
            if mc is None or not mc[0].get("course_name"):
                bad_rows += 1
                continue
            entry = mc[0]
            # R66：数字字段（xf/zxs）归一为字符串（模型 str 契约）
            for _k in ("credit", "total_hours"):
                if entry.get(_k) is not None:
                    entry[_k] = str(entry[_k])
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            entry["entry_id"] = "jwglxt-catalog-" + _hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            entry["source_id"] = source
            if kw and kw not in (entry.get("course_name") or ""):
                continue   # keyword 本地筛选（标题包含）
            mapped.append(entry)
        issues = [E.issue("completeness_unproven", False,
                          "全校课程库（来源声明 %s 条，本页 %d 条）；keyword 为本地筛选"
                          "（当前页内匹配），跨页检索请翻页" % (fr.total_expected, len(mapped)))]
        if bad_rows:
            issues.append(E.issue("upstream_error", False,
                                  "%d 行结构异常已具名跳过" % bad_rows))
        _out = ("partial_source" if (bad_rows and fr.outcome == "ok") else fr.outcome)
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=_out,
            diag=(fr.diag + ("；%d 行结构异常已具名跳过" % bad_rows) if bad_rows
                  else fr.diag),
            total=fr.total_expected, bad_rows=bad_rows, issues=issues, now=now,
            limit=limit, parser_version=parser_version, evidence_ref=evidence_ref)


    # ---------- B01-04：学生预选信息查询（N255585 viewFunc） ----------

    _PRESELECT_FIELDS = {
        "kch": "course_code", "kcmc": "course_name", "xf": "credit",
        "xh": "student_id", "xm": "student_name", "xy": "school",
        "zy": "major", "nj": "grade", "xnm": "academic_year", "xqm": "semester",
    }

    def get_preselect_info(self, xnm: str | None = None, xqm: str | None = None,
                           cursor: str | None = None, limit: int = 20) -> dict:
        """B01-04：get_preselect_info（学生预选信息查询，教务网页版本人视角）。
        返回本人预选课程记录（课程/学分/学院/专业/年级）。学期缺省自动解析。"""
        from ..adapters.campus.jwweb import fetch_preselect_info
        module, source = MODULE_PRESELECT_INFO, SOURCE_JWGLXT
        parser_version, evidence_ref = PARSER_VERSION_JWWEB, EVIDENCE_REF_JWWEB
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, "preselect",
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     "https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                                     "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N255585",
                                     parser_version, evidence_ref)
        acct, _cookies, transport, err = self._jwweb_ctx(module, source, "preselect")
        if err is not None:
            return err
        safe_url = ("https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                    "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N255585")
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._serve_cursor(acct, module, source, "",
                                      cursor, limit, now, safe_url,
                                      parser_version, evidence_ref, "jwweb")
        # 学期缺省自动解析（不猜值）
        if not xnm or not xqm:
            tr = fetch_current_term(transport)
            if tr["outcome"] != "ok":
                return self._unavailable(module, source, "preselect",
                                         E.issue("upstream_error", False,
                                                 "当前学期解析失败（%s：%s）；请显式传 xnm/xqm"
                                                 % (tr["outcome"], tr["diag"])),
                                         safe_url, parser_version, evidence_ref)
            xnm = xnm or tr["term"]
            xqm = xqm or tr.get("term_xqm")
            if not xnm or not xqm:
                return self._unavailable(module, source, "preselect",
                                         E.issue("upstream_error", False,
                                                 "当前学年和/或学期码未解析到；请显式传 xnm/xqm"),
                                         safe_url, parser_version, evidence_ref)
        scope = "preselect:%s-%s" % (xnm, xqm)
        self.store.record_attempt(acct, module, source, now_s, scope)
        fr = fetch_preselect_info(transport, xnm, xqm)
        if fr.outcome == "session_expired":
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "教务网页版会话已失效：%s" % fr.diag),
                                     safe_url, parser_version, evidence_ref)
        if fr.outcome in ("http_error", "network_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", fr.outcome == "network_error",
                                            "jwglxt %s" % fr.outcome))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", True,
                                             "预选信息读取失败（%s：%s）" % (fr.outcome, fr.diag)),
                                     safe_url, parser_version, evidence_ref)
        import hashlib as _hashlib
        mapped = []
        bad_rows = 0
        for row in (fr.items or []):
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            mc, _v = _map_items([row], self._PRESELECT_FIELDS,
                                require_course_name=False)
            if mc is None or not mc[0].get("course_name"):
                bad_rows += 1
                continue
            entry = mc[0]
            # R66：数字字段（credit）归一为字符串（模型 str 契约）
            if entry.get("credit") is not None:
                entry["credit"] = str(entry["credit"])
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            entry["entry_id"] = "jwglxt-preselect-" + _hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            entry["source_id"] = source
            mapped.append(entry)
        issues = [E.issue("completeness_unproven", False,
                          "预选信息为来源当前返回（本人视角，学期 %s-%s）；"
                          "来源声明 %s 条" % (xnm, xqm, fr.total_expected))]
        if bad_rows:
            issues.append(E.issue("upstream_error", False,
                                  "%d 行结构异常已具名跳过" % bad_rows))
        _out = ("partial_source" if (bad_rows and fr.outcome == "ok") else fr.outcome)
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=_out,
            diag=(fr.diag + ("；%d 行结构异常已具名跳过" % bad_rows) if bad_rows
                  else fr.diag),
            total=fr.total_expected, bad_rows=bad_rows, issues=issues, now=now,
            limit=limit, parser_version=parser_version, evidence_ref=evidence_ref)


    # ---------- F21：图书馆馆藏检索 ----------

    _LIB_SEARCH_TYPES = {"book": "图书", "jour": "期刊", "thesis": "学位论文"}

    def get_library_search(self, keyword: str = "", search_type: str = "book",
                           detail_url: str | None = None,
                           cursor: str | None = None,
                           limit: int = 20) -> dict:
        """F21：get_library_search（图书馆馆藏检索，superlib 聚合，匿名公开源）。
        keyword=检索关键字；search_type ∈ book/图书、jour/期刊、thesis/学位论文。
        detail_url=书目详情链接（检索结果里的 url）时返回该书目详情（正文+字段）。
        返回书目列表（标题+详情链接）+各类型统计。期刊/学位论文端点当前无条目（如实）。"""
        from ..adapters.campus.mypage import fetch_library_search
        module, source = "library.search", "src.library"
        # R67：keyword+search_type 均入 scope（不共用缓存）
        scope = ("library-search:%s" % (keyword or "").strip()
                 + (":%s" % (search_type or "book")))
        safe_url = "http://book.zjgsu.superlib.net/searchMix"
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        # F21：detail_url → 书目详情（正文+字段提取，host 白名单）
        if detail_url:
            from ..adapters.campus.mypage import fetch_library_book_detail
            # R59 结论：bookDetail 链接的 d= 签名与 dxNumber 绑定会话且检索结果
            # 每次变化——跨调用复现受限（适配器会重检索刷新，首页未命中则如实失败）
            res_d = fetch_library_book_detail(detail_url, keyword=(keyword or "").strip())
            if res_d["outcome"] != "ok":
                return self._unavailable(module, source, "book-detail",
                                         E.issue("upstream_error", False,
                                                 "书目详情读取失败（%s：%s）"
                                                 % (res_d["outcome"], res_d["diag"])),
                                         detail_url, "lib-parser/0.1.0",
                                         "validation/mypage/取证记录.md")
            text = res_d["text"] or ""
            # 字段提取（详情页固定标签：【ISBN号】【中图法分类号】等）
            def _field(label):
                i = text.find(label)
                # R68：位置 0 也取值（不漏）
                return (text[i + len(label):i + len(label) + 40]
                        .split("【")[0].split("|")[0].strip() or None) if i >= 0 else None
            fields = {"isbn": _field("【ISBN号】"),
                      "clc_no": _field("【中图法分类号】"),
                      "price": _field("【原书定价】"),
                      "publish": _field("出版项】")}
            from ..adapters.campus.mypage import _sanitize_url as _su
            url_clean, _h2 = _su(detail_url)
            return {
                "status": "ok", "module_id": module,
                "items": [{"entry_id": "lib-book-detail", "title": None,
                           "url": url_clean, "keyword": None,
                           "detail_text": text, **fields, "source_id": source}],
                "coverage": {"source_snapshots": []},
                "pagination": {"has_more": False, "next_cursor": None,
                               "result_snapshot_id": None},
                "freshness": {"last_attempt_at": now_s,
                              "last_success_at": now_s, "data_age_seconds": 0,
                              "is_stale": None, "by_source": []},
                "source_refs": [],
                "issues": [E.issue("completeness_unproven", False,
                                   "书目详情原文（4000 字内）+关键字段；馆藏地/可借量"
                                   "在另一系统（个人借阅类），未接")],
            }
        if not (keyword or "").strip():
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "keyword 须为检索关键字"),
                                     safe_url, "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "馆藏检索暂不支持分页续读（cursor 不消费）"
                                             "——请修改关键字重新检索"),
                                     safe_url, "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        st = (search_type or "book").strip()
        # R59 修：st 可为代码或中文——归一到代码
        st_code = {v: k for k, v in self._LIB_SEARCH_TYPES.items()}.get(st, st)
        if st_code not in self._LIB_SEARCH_TYPES:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "search_type 须为：图书（book）/期刊（jour）/"
                                             "学位论文（thesis）"),
                                     safe_url, "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..100 的整数"),
                                     safe_url, "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        res = fetch_library_search((keyword or "").strip(), search_type=st_code)
        if res["outcome"] in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "馆藏检索失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "馆藏检索失败（%s：%s）" % (res["outcome"], res["diag"])),
                                     safe_url, "lib-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        for row in (res["items"] or []):
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "lib-search-" + _hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            row["source_id"] = source
            row["keyword"] = (keyword or "").strip()
            mapped.append(row)
        issues = [E.issue("completeness_unproven", False,
                          "superlib 聚合检索结果（匿名公开源）；"
                          "类型统计：%s" % (json.dumps(res.get("counts") or {},
                                                        ensure_ascii=False) or "无"))]
        _res = self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=None, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="lib-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md")
        # R67 残余：检索结果不发出 cursor（消费端不支持续读）
        _res["pagination"]["has_more"] = False
        _res["pagination"]["next_cursor"] = None
        return _res


    # ---------- F06：教务处通知/校历/当前周次 ----------

    def get_jww_notices(self, detail_url: str | None = None,
                        keyword: str | None = None, page: int = 1,
                        cursor: str | None = None, limit: int = 20) -> dict:
        """F06：get_jww_notices（教务处事务通知列表/正文/当前教学周，匿名公开源）。
        keyword=标题筛选；detail_url 传通知链接返回正文原文（6000 字内）并计算
        当前教学周（解析「X月X日起正式上课」）。教务处网站 jww.zjgsu.edu.cn。"""
        from ..adapters.campus.mypage import (fetch_jww_notices,
                                              fetch_jww_notice_detail,
                                              compute_current_week)
        module, source = "jww.notices", "src.jww"
        scope = "jww-notices:p%d" % max(1, page or 1)
        safe_url = "https://jww.zjgsu.edu.cn/1335/list.htm"
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        # R69：cursor 拒绝提前到 record_attempt 前（不发不消费的 cursor）
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "教务处通知暂不支持分页续读（cursor 不消费）"
                                             "——请用 page 翻页"),
                                     safe_url, "jww-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        # R69：limit/page 边界校验
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..100 的整数"),
                                     safe_url, "jww-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        if isinstance(page, bool) or not isinstance(page, int) or not (1 <= page <= 50):
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "page 须为 1..50 的整数"),
                                     safe_url, "jww-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        if detail_url:
            res_d = fetch_jww_notice_detail(detail_url)
            if res_d["outcome"] != "ok":
                return self._unavailable(module, source, scope,
                                         E.issue("upstream_error", False,
                                                 "通知正文读取失败（%s：%s）"
                                                 % (res_d["outcome"], res_d["diag"])),
                                         detail_url, "jww-parser/0.1.0",
                                         "validation/mypage/取证记录.md")
            text = res_d["text"] or ""
            week_info = None
            # 解析教学时间安排（含「起全校正式上课」的段落）
            i = text.find("起全校正式上课")
            if i > 0:
                seg = text[max(0, i - 120):i + 20]
                week_info = compute_current_week(seg)
            issues = [E.issue("completeness_unproven", False,
                              "教务处通知正文原文（6000 字内）"
                              + ("；当前教学周第 %s 周（开学 %s）"
                                 % (week_info["week"], week_info["start"])
                                 if week_info and week_info.get("week") else ""))]
            from ..adapters.campus.mypage import _sanitize_url as _su
            url_clean, _h3 = _su(detail_url)
            return {
                "status": "ok", "module_id": module,
                "items": [{"entry_id": "jww-notice-detail", "title": None,
                           "url": url_clean, "date": None,
                           "detail_text": text,
                           "current_week": (str(week_info["week"])
                                            if week_info and week_info.get("week")
                                            else None),
                           "semester_start": (week_info["start"]
                                              if week_info else None),
                           "source_id": source}],
                "coverage": {"source_snapshots": []},
                "pagination": {"has_more": False, "next_cursor": None,
                               "result_snapshot_id": None},
                "freshness": {"last_attempt_at": now_s,
                              "last_success_at": now_s, "data_age_seconds": 0,
                              "is_stale": None, "by_source": []},
                "source_refs": [],
                "issues": issues,
            }
        res = fetch_jww_notices(page=page)
        if res["outcome"] in ("network_error", "http_error", "parse_error",
                              "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "教务处通知读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "教务处通知读取失败（%s：%s）"
                                             % (res["outcome"], res["diag"])),
                                     safe_url, "jww-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        kw = (keyword or "").strip()
        for row in (res["items"] or []):
            if kw and kw not in (row.get("title") or ""):
                continue
            digest = json.dumps(row, ensure_ascii=False, sort_keys=True)
            row["entry_id"] = "jww-notice-" + _hashlib.sha1(
                digest.encode("utf-8")).hexdigest()[:16]
            row["source_id"] = source
            row["detail_text"] = None
            mapped.append(row)
        issues = [E.issue("completeness_unproven", False,
                          "教务处事务通知（匿名公开源）；detail_url 传链接可取正文"
                          "并计算当前教学周")]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=None, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="jww-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)


    # ---------- F17：体测成绩 ----------

    def get_fitness_scores(self, cursor: str | None = None, limit: int = 20) -> dict:
        """F17：get_fitness_scores（体测成绩，tzjk.zjgsu.edu.cn 本人视角）。
        需要体测专用 token（独立导入：python -m src.auth.login_cli fitness-token）；单次体测记录返回。"""
        from ..adapters.campus.mypage import fetch_fitness_scores
        module, source = "fitness.scores", "src.tzjk"
        scope = "fitness"
        safe_url = "https://tzjk.zjgsu.edu.cn/subscribe/home.html"
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        # R70：上下文一次绑定（_ctx 的 account/session 全程使用，不中途重读 active）
        sess_info = session0 or {}
        token = sess_info.get("tzjk_token")
        student_no = sess_info.get("tzjk_student_no")
        if not token or not student_no:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "体测 token 未建立：请在 App 打开体测页后"
                                             "用 login_cli 的 fitness-token 命令导入令牌"
                                             "（原 init 不包含体测会话）",
                                             auth_domain="independent"),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        acct = acct0
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "体测成绩暂不支持分页续读"),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_fitness_scores(token, student_no)
        if res["outcome"] in ("network_error", "http_error", "parse_error", "upstream_error"):
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "体测读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "体测成绩读取失败（%s：%s）"
                                             % (res["outcome"], res["diag"])),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        # R70：合法空（ok_empty）→ mapped=[]（0 条，不造全 null 记录）
        if res["outcome"] == "ok_empty":
            issues = [E.issue("completeness_unproven", False,
                              "来源已确认无成绩记录（合法空 0 条）")]
            return self._finalize_public_success(
                acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
                mapped=[], outcome="ok_empty", diag=res.get("diag") or "",
                total=0, bad_rows=0, issues=issues, now=now, limit=limit,
                parser_version="tzjk-parser/0.1.0",
                evidence_ref="validation/mypage/取证记录.md")
        data = res.get("data") or {}
        # R70：数字字段归一为字符串（模型 str 契约——普通数字/零/缺失三类覆盖）
        def _s(v):
            return None if v is None else str(v)
        entry = {
            "entry_id": "tzjk-" + str(data.get("id") or "0"),
            "exam": _s(data.get("exam")), "grade": _s(data.get("grade")),
            "gender": _s(data.get("gender")), "height": _s(data.get("height")),
            "weight": _s(data.get("weight")), "bmi": _s(data.get("bmi")),
            "vital_capacity": _s(data.get("vitalCapacity")),
            "fifty_meter": _s(data.get("fiftyMeter")),
            "standing_long_jump": _s(data.get("standingLongJump")),
            "sit_and_reach": _s(data.get("sitAndReach")),
            "pull_up": _s(data.get("pullUp")),
            "thousand_meter": _s(data.get("thousandMeter")),
            "score": _s(data.get("score")), "rank": _s(data.get("rank")),
            "student_no": _s(data.get("studentNo")), "name": _s(data.get("name")),
            "source_id": source,
        }
        issues = [E.issue("completeness_unproven", False,
                          "体测成绩（本人视角，单次体测记录）；"
                          "体育课成绩与历年对比不在本来源")]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=[entry], outcome=res["outcome"], diag=res.get("diag") or "",
            total=1 if data else 0, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="tzjk-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)


    # ---------- F13：综合测评结果 ----------

    def get_evaluation_result(self, cursor: str | None = None, limit: int = 20) -> dict:
        """F13：get_evaluation_result（综合测评结果，小鸿书综测系统本人视角）。
        测评周期外来源返回无权限（如实 unavailable）；不代申报/修改/审核。"""
        from ..adapters.campus.mypage import (fetch_xszkdh_token,
                                              fetch_evaluation_result)
        module, source = "evaluation.result", "src.zbp"
        scope = "evaluation"
        safe_url = "https://xszkdh.zjgsu.edu.cn/zbp-evaluation-mobile/"
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     safe_url, "zbp-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        # R70：上下文一次绑定（同体测——不中途重读 active）
        sess_info = session0 or {}
        acct = acct0
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "综测结果暂不支持分页续读"),
                                     safe_url, "zbp-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        tk = fetch_xszkdh_token(sess_info.get("jwglxt_cookies"))
        if tk["outcome"] != "ok":
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", False,
                                            "综测 token 获取失败（%s）" % tk["diag"]))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", False,
                                             "综测 token 获取失败（%s）" % tk["diag"]),
                                     safe_url, "zbp-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        res = fetch_evaluation_result(tk["token"], tk["role"])
        if res["outcome"] != "ok":
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", False,
                                            "综测结果读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", False,
                                             "综测结果读取失败（%s：%s）"
                                             % (res["outcome"], res["diag"])),
                                     safe_url, "zbp-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        # R71：输出白名单——综测真实成功字段未核实，来源键不透传
        #（认证标记/未知键不进响应/缓存；entry_id/source_id 本机生成不可覆盖）
        data = res.get("data") or {}
        if data:
            # R72：非空来源数据但白名单未建立 → parse_error（不当合法空覆旧/推进）
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("parse_error", False,
                                            "综测来源有数据但业务字段白名单未建立"
                                            "——受控拒绝，不透传不落库"))
            return self._unavailable(module, source, scope,
                                     E.issue("parse_error", False,
                                             "综测结果字段白名单未建立（来源有数据，"
                                             "受控拒绝不透传）——等待真实成功样本核实"),
                                     safe_url, "zbp-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        entry = {"entry_id": "zbp-evaluation", "source_id": source}
        issues = [E.issue("completeness_unproven", False,
                          "综合测评结果（本人视角）；来源拒绝时如实返回受控状态；"
                          "不代申报/修改/审核")]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=[entry], outcome=res["outcome"], diag=res.get("diag") or "",
            total=1, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="zbp-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)


    # ---------- F17 扩展：体测预约时段 ----------

    def get_fitness_schedule(self, cursor: str | None = None, limit: int = 20) -> dict:
        """F17 扩展：get_fitness_schedule（体测预约时段——学年/有效日期/每日时段）。
        token 来源同 get_fitness_scores（App 会话）。只读，不代预约。"""
        from ..adapters.campus.mypage import fetch_fitness_schedule
        module, source = "fitness.schedule", "src.tzjk"
        scope = "fitness-schedule"
        safe_url = "https://tzjk.zjgsu.edu.cn/subscribe/home.html"
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        # R70：上下文一次绑定（同 get_fitness_scores）
        sess_info = session0 or {}
        token = sess_info.get("tzjk_token")
        if not token:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "体测 token 未建立：请在 App 打开体测页后"
                                             "用 login_cli 的 fitness-token 命令导入令牌"
                                             "（原 init 不包含体测会话）",
                                             auth_domain="independent"),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        acct = acct0
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "体测时段暂不支持分页续读"),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_fitness_schedule(token)
        if res["outcome"] != "ok":
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "体测时段读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "体测时段读取失败（%s：%s）"
                                             % (res["outcome"], res["diag"])),
                                     safe_url, "tzjk-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        d = res.get("data") or {}
        exam = d.get("exam") or {}
        entry = {
            "entry_id": "tzjk-schedule-" + str(d.get("id") or "0"),
            "exam_name": exam.get("name"),
            "exam_year": (str(exam["year"]) if exam.get("year") is not None else None),
            "subscribe_begin": d.get("beginTime"),
            "subscribe_end": d.get("endTime"),
            "exam_begin": exam.get("beginTime"),
            "exam_end": exam.get("endTime"),
            "morning_begin": exam.get("morningBeginTime"),
            "morning_end": exam.get("morningEndTime"),
            "afternoon_begin": exam.get("afternoonBeginTime"),
            "afternoon_end": exam.get("afternoonEndTime"),
            "enabled": str(exam.get("enabled")),
            "source_id": source,
        }
        issues = [E.issue("completeness_unproven", False,
                          "体测预约时段（本人视角）；预约操作不提供（仅时段查询）")]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=[entry], outcome=res["outcome"], diag=res.get("diag") or "",
            total=1, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="tzjk-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)


    # ---------- F16：劳动教育课程中心 ----------

    _LABOR_FIELDS = {
        "projectName": "project_name", "deptNames": "dept_names",
        "startTime": "start_time", "endTime": "end_time",
        "applyNum": "apply_num", "residue": "residue",
        "projectType": "project_type", "isApply": "is_apply",
        "address": "address",
    }

    def get_labor_courses(self, cursor: str | None = None, limit: int = 20) -> dict:
        """F16：get_labor_courses（劳动教育课程中心项目列表，sjjx 本人视角）。
        返回课程项目（名称/学院/时间/报名人数/剩余名额/分类/是否已报）。
        报名/取消/提交不做（只读）。"""
        from ..adapters.campus.mypage import fetch_labor_courses
        module, source = "labor.courses", "src.sjjx"
        scope = "labor-courses"
        safe_url = "https://sjjx.zjgsu.edu.cn/teachApp/pages/curriculumCenter/index"
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     safe_url, "sjjx-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        acct = acct0
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "劳动课程暂不支持分页续读"),
                                     safe_url, "sjjx-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_labor_courses(session0.get("jwglxt_cookies"))
        if res["outcome"] != "ok":
            # B03 反馈 P2：失败受控 + 失败保旧
            issue = E.issue("upstream_error", res["outcome"] == "network_error",
                            "劳动课程读取失败（%s：%s）"
                            % (res["outcome"], res["diag"]))
            self.store.record_issue(acct, module, source, now_s, scope, issue)
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"], old.get("items") or [], 0, limit)
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": {
                        "last_attempt_at": now_s, "last_success_at": old_ls,
                        "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                        "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                       "last_success_at": old_ls,
                                       "data_age_seconds": _age_seconds(now, old_ls),
                                       "is_stale": None}]},
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     "sjjx-parser/0.1.0",
                                                     "validation/mypage/取证记录.md"),
                    "issues": [issue, E.issue("completeness_unproven", False,
                                              "已返回既有成功快照；本次失败观测未入库")],
                }
            return self._unavailable(module, source, scope, issue,
                                     safe_url, "sjjx-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        import hashlib as _hashlib
        mapped = []
        bad_rows = 0
        for row in (res.get("items") or []):
            if not isinstance(row, dict) or not row.get("projectName"):
                bad_rows += 1
                continue
            entry = {"entry_id": "sjjx-labor-" + str(row.get("id") or "")[:24],
                     "project_id": str(row.get("id") or "")}
            for src, dst in self._LABOR_FIELDS.items():
                v = row.get(src)
                entry[dst] = None if v is None else str(v)
            entry["source_id"] = source
            mapped.append(entry)
        issues = [E.issue("completeness_unproven", False,
                          "劳动教育课程中心项目列表（本人视角，首屏）；"
                          "课程单元详情/打卡记录/报名操作不提供；"
                          "来源 total=%s" % res.get("total"))]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=res.get("total"), bad_rows=bad_rows, issues=issues, now=now,
            limit=limit, parser_version="sjjx-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)


    # ---------- B03：劳动课程详情+本人记录 ----------

    def get_labor_course_detail(self, project_id: str,
                                cursor: str | None = None, limit: int = 20) -> dict:
        """B03：get_labor_course_detail（劳动课程详情+本人报名/时数/完成记录）。
        project_id 来自 get_labor_courses 的 project_id 字段。只读，不提交。"""
        from ..adapters.campus.mypage import fetch_labor_course_detail
        module, source = "labor.course_detail", "src.sjjx"
        if not (project_id or "").strip():
            return self._unavailable(module, source, "labor-detail",
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "project_id 须来自 get_labor_courses"),
                                     "https://sjjx.zjgsu.edu.cn/teachApp/"
                                     "pages/curriculumCenter/index",
                                     "sjjx-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        scope = "labor-detail:%s" % project_id.strip()[:40]
        safe_url = ("https://sjjx.zjgsu.edu.cn/teachApp/pages/curriculumCenter/index")
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"),
                                     safe_url, "sjjx-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        acct = acct0
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "劳动详情暂不支持分页续读"),
                                     safe_url, "sjjx-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_labor_course_detail(session0.get("jwglxt_cookies"),
                                        project_id.strip())
        if res["outcome"] not in ("ok", "ok_empty"):
            # B03 反馈 P2：失败受控 + 失败保旧（有旧成功快照 → 返回旧条目，不覆盖）
            issue = E.issue("upstream_error", res["outcome"] == "network_error",
                            "劳动详情读取失败（%s：%s）"
                            % (res["outcome"], res["diag"]))
            self.store.record_issue(acct, module, source, now_s, scope, issue)
            old = self.store.get_snapshot(acct, module, source, scope)
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope, old["snapshot_id"], old.get("items") or [], 0, limit)
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": {
                        "last_attempt_at": now_s, "last_success_at": old_ls,
                        "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                        "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                       "last_success_at": old_ls,
                                       "data_age_seconds": _age_seconds(now, old_ls),
                                       "is_stale": None}]},
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     "sjjx-parser/0.1.0",
                                                     "validation/mypage/取证记录.md"),
                    "issues": [issue, E.issue("completeness_unproven", False,
                                              "已返回既有成功快照；本次失败观测未入库")],
                }
            return self._unavailable(module, source, scope, issue,
                                     safe_url, "sjjx-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        detail = res.get("detail") or {}
        mine = res.get("mine") or {}
        # R72 同款纪律：显式白名单投影（来源键不透传），标量 str 归一
        detail_fields = {}
        for k in ("projectName", "deptNames", "startTime", "endTime",
                  "address", "applyNum", "residue", "projectType", "content"):
            v = detail.get(k)
            detail_fields[k] = None if v is None else (
                str(v) if not isinstance(v, (dict, list)) else None)
        mine_fields = {}
        for k in ("courseName", "startTime", "endTime", "status", "times",
                  "completedTimes", "teacherName", "place"):
            v = mine.get(k)
            mine_fields[k] = None if v is None else (
                str(v) if not isinstance(v, (dict, list)) else None)
        entry = {
            "entry_id": "sjjx-detail-" + str(hash(project_id) & 0xFFFFFFFF),
            "project_id": project_id.strip(),
            "source_id": source,
            **detail_fields,
            **{("mine_" + k): v for k, v in mine_fields.items()},
        }
        notes = []
        if res.get("detail_note"):
            notes.append("详情：%s" % res["detail_note"])
        if res.get("mine_note"):
            notes.append("本人记录：%s" % res["mine_note"])
        empty_note = ("来源明确未查询到（合法空）；本次观测覆盖同 scope 旧快照"
                      if res["outcome"] == "ok_empty" else "来源无补充说明")
        issues = [E.issue("completeness_unproven", False,
                          "劳动课程详情+本人记录（本人视角，单项目）；"
                          + ("；".join(notes) if notes else empty_note)
                          + "；报名/提交不做")]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=[entry], outcome=res["outcome"], diag=res.get("diag") or "",
            total=1, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="sjjx-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)


    # ---------- B03-③：学生手册 ----------

    def get_student_handbook(self, keyword: str | None = None,
                             cursor: str | None = None, limit: int = 20) -> dict:
        """B03-③：get_student_handbook（学生手册正文，2021 汇编版 PDF 公开源）。
        keyword=按章节标题/正文检索（返回命中页与上下文）；缺省返回目录。
        版本说明：公开网络可得 2021 汇编版；如学校发布新版本以官方为准。"""
        from ..adapters.campus.mypage import fetch_student_handbook
        module, source = "handbook.student", "src.xsc"
        scope = "student-handbook:%s" % (keyword or "").strip()[:30]
        safe_url = "https://xsc.zjgsu.edu.cn/2026/0920/c1825a231473/page.htm"
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, scope,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "学生手册暂不支持分页续读（用 keyword 检索）"),
                                     safe_url, "handbook-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        self.store.record_attempt(acct, module, source, now_s, scope)
        res = fetch_student_handbook(keyword or "")
        if res["outcome"] != "ok":
            self.store.record_issue(acct, module, source, now_s, scope,
                                    E.issue("upstream_error", res["outcome"] == "network_error",
                                            "学生手册读取失败（%s：%s）"
                                            % (res["outcome"], res["diag"])))
            return self._unavailable(module, source, scope,
                                     E.issue("upstream_error", res["outcome"] == "network_error",
                                             "学生手册读取失败（%s：%s）"
                                             % (res["outcome"], res["diag"])),
                                     safe_url, "handbook-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        issues = [E.issue("completeness_unproven", False,
                          "学生手册 2026 版（2026-09-20 学生处发布页公开 PDF，318 页；"
                          "官方发布出处见 source_refs.safe_entry_url）；"
                          "如学校发布新版本以官方为准；纸质版为最终权威；"
                          "keyword 检索最多返回前 5 处命中（截断）")]
        if res.get("sections") is not None:
            mapped = [{"entry_id": "handbook-kw-%d" % (i + 1),
                       "page": str(sec.get("page")),
                       "context": sec.get("context"),
                       "version": "2026", "source_id": source}
                      for i, sec in enumerate(res["sections"])]
        else:
            mapped = [{"entry_id": "handbook-toc", "page": None,
                       "context": (res.get("toc") or "")[:6000],
                       "version": "2026", "source_id": source}]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=len(mapped), bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="handbook-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)

    # ---------- T1：F11 本人流程申请列表（泛微 Ecology OA，flow.zjgsu） ----------

    def get_my_approvals(self, scope: str = "mine", cursor: str | None = None,
                         limit: int = 20) -> dict:
        """F11：get_my_approvals（本人流程申请列表，泛微 Ecology OA）。

        scope=mine（我的请求，默认）/ doing（待办事宜）/ done（已办事宜）。
        只读列表；同意/转交/撤回/退回等写操作不做。CAS TGT 失效 → 提示重跑 login_cli init。"""
        from ..adapters.campus.flow import fetch_my_requests as _fetch, VIEW_SCOPES as _SCOPES
        module, source = MODULE_APPROVALS, SOURCE_FLOW
        safe_url = "https://flow.zjgsu.edu.cn/spa/workflow/static4form/index.html"
        scope_key = "flow:%s" % scope
        if scope not in _SCOPES:
            return self._unavailable(module, source, scope_key,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "scope 须为 mine（我的请求）/doing（待办）/done（已办）"),
                                     safe_url, "flow-parser/0.1.0",
                                     "validation/flow/取证记录.md")
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 500):
            return self._unavailable(module, source, scope_key,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "limit 须为 1..500 的整数"),
                                     safe_url, "flow-parser/0.1.0",
                                     "validation/flow/取证记录.md")
        acct0, session0 = self._ctx()
        if not acct0 or not session0:
            return self._unavailable(module, source, scope_key,
                                     E.issue(E.AUTH_REQUIRED, False,
                                             "本地无有效会话：请先在本机运行登录初始化"
                                             "（python -m src.auth.login_cli init）"),
                                     safe_url, "flow-parser/0.1.0",
                                     "validation/flow/取证记录.md")
        if cursor:
            return self._unavailable(module, source, scope_key,
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "流程列表暂不支持分页续读"
                                             "（来源 sessionkey 短时效，未接续读）"),
                                     safe_url, "flow-parser/0.1.0",
                                     "validation/flow/取证记录.md")
        acct = acct0
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        self.store.record_attempt(acct, module, source, now_s, scope_key)
        res = _fetch(session0.get("jwglxt_cookies"), view_scope=scope,
                     current=1, page_size=min(limit, 100))
        if res["outcome"] != "ok" and res["outcome"] != "ok_empty":
            diag = res.get("diag") or res["outcome"]
            if "登录态未建立" in diag:
                issue = E.issue(E.AUTH_REQUIRED, False,
                                "flow 会话未建立（%s）；请重新运行 "
                                "python -m src.auth.login_cli init 刷新 CAS 会话" % diag)
                code = E.AUTH_REQUIRED
            else:
                issue = E.issue("upstream_error", res["outcome"] == "network_error",
                                "流程列表读取失败（%s：%s）" % (res["outcome"], diag))
                code = "upstream_error"
            self.store.record_issue(acct, module, source, now_s, scope_key, issue)
            return self._unavailable(module, source, scope_key, issue,
                                     safe_url, "flow-parser/0.1.0",
                                     "validation/flow/取证记录.md")
        mapped = []
        for row in (res.get("items") or []):
            mapped.append({
                "entry_id": "flow-req-" + (row.get("request_id") or "")[:24],
                "request_id": row.get("request_id"),
                "request_name": row.get("request_name"),
                "create_date": row.get("create_date"),
                "workflow_id": row.get("workflow_id"),
                "view_scope": scope,
                "source_id": source,
            })
        issues = [E.issue("completeness_unproven", False,
                          "本人流程列表（泛微 Ecology OA，%s 视图，首屏 %d 条）；"
                          "来源未提供总数与结束证据；工作流名称/当前节点来源本视图不回——不造字段；"
                          "同意/转交/撤回等写操作不做"
                          % (scope, len(mapped)))]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope_key, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=None, bad_rows=res.get("bad_rows") or 0, issues=issues, now=now,
            limit=limit, parser_version="flow-parser/0.1.0",
            evidence_ref="validation/flow/取证记录.md", emit_cursor=False)

    # ---------- T2：F14 学生处公开公示（评奖评优/资助评审公示） ----------

    def get_scholarship_publications(self, keyword: str | None = None, page: int = 1,
                                     detail_url: str | None = None,
                                     cursor: str | None = None, limit: int = 20) -> dict:
        """F14：get_scholarship_publications（学生处公开公示栏检索）。

        评奖评优/资助评审结果以公示为准（本人申请进度走 get_my_approvals）。
        keyword=标题筛选；page 翻页；detail_url=公示详情正文+附件。"""
        from ..adapters.campus.mypage import fetch_scholarship_publications as _fetch
        module, source = MODULE_AWARDS_PUBLICATIONS, SOURCE_XSC_PUB
        safe_url = "https://xsc.zjgsu.edu.cn/1827/list.htm"
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, "xsc-pub",
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "公示检索用 page 参数翻页（不支持游标续读）"),
                                     safe_url, "xsc-pub-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        if detail_url is not None and not isinstance(detail_url, str):
            return self._unavailable(module, source, "xsc-pub",
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "detail_url 须为字符串或省略"),
                                     safe_url, "xsc-pub-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        scope_key = ("xsc-pub:%s" % detail_url if detail_url else
                     "xsc-pub:p%s:%s" % (page, (keyword or "").strip()[:20]))
        self.store.record_attempt(acct, module, source, now_s, scope_key)
        res = _fetch(keyword=keyword or "", page=page,
                     detail_url=detail_url or "")
        if res["outcome"] != "ok" and res["outcome"] != "ok_empty":
            # B03 r2 反馈②：详情/列表失败受控 + 保旧（不覆盖旧成功内容）
            issue = E.issue("upstream_error", res["outcome"] == "network_error",
                            "公示读取失败（%s：%s）" % (res["outcome"], res.get("diag") or ""))
            self.store.record_issue(acct, module, source, now_s, scope_key, issue)
            old = self.store.get_snapshot(acct, module, source, scope_key)
            if old is not None:
                page2, has_more, next_cursor = self._page(
                    acct, module, scope_key, old["snapshot_id"], old.get("items") or [], 0, limit)
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                return {
                    "status": "partial", "module_id": module, "items": page2,
                    "coverage": {"source_snapshots": [self._snap_block(scope_key, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": {
                        "last_attempt_at": now_s, "last_success_at": old_ls,
                        "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                        "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                       "last_success_at": old_ls,
                                       "data_age_seconds": _age_seconds(now, old_ls),
                                       "is_stale": None}]},
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     "xsc-pub-parser/0.1.0",
                                                     "validation/mypage/取证记录.md"),
                    "issues": [issue, E.issue("completeness_unproven", False,
                                              "已返回既有成功快照；本次失败观测未入库")],
                }
            return self._unavailable(module, source, scope_key, issue,
                                     safe_url, "xsc-pub-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        if res.get("detail") is not None:
            mapped = [{"entry_id": "xsc-pub-detail-" + str(hash(detail_url) & 0xFFFFFFFF),
                       "title": None, "url": detail_url, "publish_date": None,
                       "text": res.get("detail"), "attachments": res.get("attachments") or [],
                       "source_id": source}]
        else:
            mapped = []
            for it in (res.get("items") or []):
                u = it.get("url") or ""
                mapped.append({
                    "entry_id": "xsc-pub-" + hashlib.md5(u.encode("utf-8")).hexdigest()[:12],
                    "title": it.get("title"), "url": u or None,
                    "publish_date": it.get("date"), "text": None,
                    "attachments": [], "source_id": source})
        issues = [E.issue("completeness_unproven", False,
                          "学生处公开公示栏（评奖评优/资助评审公示，匿名公开源，第 %s 页）；"
                          "获奖结果以公示原文与学校正式文件为准；keyword 仅当前页内过滤"
                          % page)]
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope_key, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=None, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="xsc-pub-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)

    # ---------- T3：F26 图书馆数字资源导航 ----------

    def get_digital_resources(self, category: str = "all", keyword: str | None = None,
                              cursor: str | None = None, limit: int = 50) -> dict:
        """F26：get_digital_resources（图书馆数字资源导航，公开静态目录）。

        category=all（聚合 8 子栏目）/chinese_journal/foreign_journal/ebook/thesis/
        econ_data/multimedia/others/trial；keyword=资源名筛选；
        access_way 为说明性推断（内网 IP→校内/VPN），不猜 CARSI 覆盖范围。"""
        from ..adapters.campus.mypage import (fetch_digital_resources as _fetch,
                                              DIGITAL_CATEGORIES as _CATS)
        module, source = MODULE_DIGITAL_RESOURCES, SOURCE_LIB
        safe_url = "http://lib.zjgsu.edu.cn/1013/list.htm"
        acct = ACCOUNT_PUBLIC
        now = self._now_fn()
        now_s = now.isoformat(timespec="seconds")
        if cursor:
            return self._unavailable(module, source, "lib-digital",
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "数字资源目录不支持游标续读（用 category/keyword）"),
                                     safe_url, "lib-digital-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        if category not in _CATS:
            return self._unavailable(module, source, "lib-digital",
                                     E.issue(E.INVALID_ARGUMENTS, False,
                                             "category 须为 all/%s" % "/".join(
                                                 k for k in _CATS if k != "all")),
                                     safe_url, "lib-digital-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        scope_key = "lib-digital:%s:%s" % (category, (keyword or "").strip()[:20])
        self.store.record_attempt(acct, module, source, now_s, scope_key)
        res = _fetch(category=category, keyword=keyword or "")
        # B03 r2 反馈：聚合部分栏目失败 → partial（缺口具名），服务层保旧；
        # 结构未证/全失败 → unavailable；keyword 无命中 → ok_empty（合法空）。
        if res.get("failed_categories") and res["outcome"] in ("ok", "ok_empty"):
            res = dict(res)
            res["outcome"] = "partial_source"
            res["diag"] = res.get("diag") or "聚合部分栏目失败"
        if res["outcome"] != "ok" and res["outcome"] != "ok_empty":
            issue = E.issue("upstream_error", res["outcome"] == "network_error",
                            "数字资源读取失败（%s：%s）" % (res["outcome"], res.get("diag") or ""))
            self.store.record_issue(acct, module, source, now_s, scope_key, issue)
            old = self.store.get_snapshot(acct, module, source, scope_key)
            if old is not None:
                page, has_more, next_cursor = self._page(
                    acct, module, scope_key, old["snapshot_id"], old.get("items") or [], 0, limit)
                health_row = self._health_row(acct, module, source)
                old_ls = health_row.get("last_success_at") if health_row else None
                return {
                    "status": "partial", "module_id": module, "items": page,
                    "coverage": {"source_snapshots": [self._snap_block(scope_key, old, source)]},
                    "pagination": {"has_more": has_more, "next_cursor": next_cursor,
                                   "result_snapshot_id": old["snapshot_id"]},
                    "freshness": {
                        "last_attempt_at": now_s, "last_success_at": old_ls,
                        "data_age_seconds": _age_seconds(now, old_ls), "is_stale": None,
                        "by_source": [{"source_id": source, "last_attempt_at": now_s,
                                       "last_success_at": old_ls,
                                       "data_age_seconds": _age_seconds(now, old_ls),
                                       "is_stale": None}]},
                    "source_refs": self._source_refs(old, source, safe_url,
                                                     "lib-digital-parser/0.1.0",
                                                     "validation/mypage/取证记录.md"),
                    "issues": [issue, E.issue("completeness_unproven", False,
                                              "已返回既有成功快照；本次部分/失败观测未入库")],
                }
            return self._unavailable(module, source, scope_key, issue,
                                     safe_url, "lib-digital-parser/0.1.0",
                                     "validation/mypage/取证记录.md")
        mapped = []
        for it in (res.get("items") or []):
            u = it.get("entry_url") or ""
            mapped.append({
                "entry_id": "lib-res-" + hashlib.md5(u.encode("utf-8")).hexdigest()[:10],
                "name": it.get("name"), "category": it.get("category"),
                "entry_url": u or None, "intro_url": it.get("intro_url"),
                "access_way": it.get("access_way"), "source_id": source})
        issues = [E.issue("completeness_unproven", False,
                          "图书馆数字资源导航（公开静态目录，%s）；条目以图书馆网站为准；"
                          "access_way 为说明性推断（内网 IP→校内网/VPN），"
                          "不推断具体资源的 CARSI/校外可达性"
                          % _CATS.get(category, ("?",))[0])]
        if res.get("failed_categories"):
            issues.append(E.issue("completeness_unproven", True,
                                  "聚合模式部分子栏目读取失败未计入：%s"
                                  % "、".join(res["failed_categories"])) )
        return self._finalize_public_success(
            acct=acct, module=module, source=source, scope=scope_key, safe_url=safe_url,
            mapped=mapped, outcome=res["outcome"], diag=res.get("diag") or "",
            total=None, bad_rows=0, issues=issues, now=now, limit=limit,
            parser_version="lib-digital-parser/0.1.0",
            evidence_ref="validation/mypage/取证记录.md", emit_cursor=False)
