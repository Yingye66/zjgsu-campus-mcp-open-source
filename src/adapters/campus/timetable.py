# -*- coding: utf-8 -*-
"""课表抓取（读取适配器）。

端点与参数已核实（代码链报告 4.1/4.2 节；10:28 单周真实读取验证）：
  POST /api/v1/noticeIntegration/mobile/studentCourse
  头：zjgsuToken；表单：ksrq / jsrq（yyyy-MM-dd，周一~周日整周口径已验证）

流程：HTTP → classify_course（已验证成功码白名单）→ schema 校验与映射。
输出 outcome 集合 = classify 词汇 + 'schema_invalid'（字段不合法，整批拒绝）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .classify import classify_course
from .schema import validate_and_map

BASE = "https://myapp.zjgsu.edu.cn"
COURSE_PATH = "/api/v1/noticeIntegration/mobile/studentCourse"


@dataclass
class TimetableFetch:
    outcome: str
    entries: list[dict] | None   # 映射后的 TimetableEntry 列表（ok 时）
    diag: str
    raw_count: int | None        # 来源返回条数（不做成功判据，仅记录）
    # F29-R6：来源业务码的结构化记录（仅本端点；供服务层受限标记恢复信号，
    # 不改变 classify 的 outcome 语义）
    raw_business_code: str | None = None


def _course_business_code(text: object) -> str | None:
    """从原始响应提取 data.code（结构化；失败返回 None，不参与成功判定）。"""
    try:
        d = json.loads(text or "")
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    data = d.get("data")
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    return None if code is None else str(code)


def fetch_timetable(transport, token: str, start_date: str, end_date: str) -> TimetableFetch:
    r = transport.post_form(BASE + COURSE_PATH,
                            headers={"zjgsuToken": token},
                            form={"ksrq": start_date, "jsrq": end_date})
    outcome, items, diag = classify_course(r.get("kind"), r.get("status", 0), r.get("text", ""))
    if outcome == "ok_empty":
        return TimetableFetch(outcome="ok_empty", entries=[], diag=diag, raw_count=0)
    if outcome != "ok":
        return TimetableFetch(outcome=outcome, entries=None, diag=diag, raw_count=None,
                              raw_business_code=_course_business_code(r.get("text")))

    entries, violations = validate_and_map(items or [])
    if entries is None:
        # 具名失败：字段/schema 不合规——整批拒绝，不产出半成品
        return TimetableFetch(
            outcome="schema_invalid", entries=None,
            diag="schema 校验未通过（%d 项）：%s" % (len(violations), "；".join(violations[:6])),
            raw_count=len(items or []))
    return TimetableFetch(outcome="ok", entries=entries, diag=diag, raw_count=len(entries))
