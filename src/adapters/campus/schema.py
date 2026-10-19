# -*- coding: utf-8 -*-
"""课表条目字段校验与映射（「不以对象数组代替业务 schema」）。

规则（对应交接文档第 2.6 节最小验收 ⑤：字段类型/日期/节次不合法时具名失败）：
- 必填：kcmc（非空 str）、xqj（1..7 整数，接受数字字符串）、ksj/jsj
  （>=1 整数、jsj>=ksj，接受数字字符串）、nyr（合法 yyyy-MM-dd）、id（非空 str）；
- 可选：其余字段缺失/空值容错（输出 None），若存在则须满足类型；
- 故意不校验：日期是否落在查询范围内、节次上限（避免用未经证据的业务规则
  误拒真实数据；本项为设计选择而非疏漏，见技术逻辑说明）。
- 违规只报「字段名 + 规则」，不回显字段值。

映射产出 TimetableEntry（见 src/domain/models.py）：
  kcmc→course_name  kcdm→course_code  kclx→course_type  xqj→weekday
  ksj→period_start  jsj→period_end    zc→week_no        nyr→date
  skrq→date_short   jsmc→location     jsbh→teacher_id（语义待核实，R22）
  xn→academic_year  xq→semester        id→entry_id
"""
from __future__ import annotations

import re
from datetime import date as _date

from ...domain.models import SOURCE_COURSE_API

_RE_FULL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_SHORT_DATE = re.compile(r"^\d{2}-\d{2}$")
_RE_INT = re.compile(r"^-?\d+$")


def _as_int(v) -> int | None:
    """整数或数字字符串 → int；其余（含 bool）→ None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and _RE_INT.match(v.strip()):
        return int(v.strip())
    return None


def _as_str(v) -> str | None:
    """str → 原样；int/float → 十进制字符串；None/空串 → None；其余 → None。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v if v != "" else None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return None


def validate_and_map(items: list[dict]) -> tuple[list[dict] | None, list[str]]:
    """校验并映射课表原始条目。

    返回 (entries, violations)：
    - 全部通过 → (entries, [])；
    - 有违规 → (None, violations)，violations 为形如 "kcmc:必填非空字符串" 的列表。
    """
    entries: list[dict] = []
    violations: list[str] = []

    for idx, it in enumerate(items, 1):
        tag = "第%d条" % idx

        # —— 必填 ——
        kcmc = it.get("kcmc")
        if not (isinstance(kcmc, str) and kcmc.strip() != ""):
            violations.append("%s:kcmc:必填非空字符串" % tag)

        xqj = _as_int(it.get("xqj"))
        if xqj is None or not (1 <= xqj <= 7):
            violations.append("%s:xqj:须为 1..7 的整数" % tag)

        ksj = _as_int(it.get("ksj"))
        jsj = _as_int(it.get("jsj"))
        if ksj is None or ksj < 1:
            violations.append("%s:ksj:须为 >=1 的整数" % tag)
        if jsj is None or jsj < 1:
            violations.append("%s:jsj:须为 >=1 的整数" % tag)
        if ksj is not None and jsj is not None and ksj >= 1 and jsj >= 1 and jsj < ksj:
            violations.append("%s:jsj:须 >= ksj" % tag)

        nyr = it.get("nyr")
        if not (isinstance(nyr, str) and _RE_FULL_DATE.match(nyr.strip())):
            violations.append("%s:nyr:须为 yyyy-MM-dd 格式" % tag)
        else:
            try:
                _date.fromisoformat(nyr.strip())
            except ValueError:
                violations.append("%s:nyr:不是合法日期" % tag)

        eid = it.get("id")
        if not (isinstance(eid, str) and eid.strip() != ""):
            violations.append("%s:id:必填非空字符串" % tag)

        # —— 可选（存在则校验类型） ——
        skrq = it.get("skrq")
        if skrq is not None and skrq != "":
            if not (isinstance(skrq, str) and _RE_SHORT_DATE.match(skrq.strip())):
                violations.append("%s:skrq:须为 MM-dd 格式" % tag)

        zc = it.get("zc")
        zc_out: int | str | None
        if zc is None or zc == "":
            zc_out = None
        else:
            zi = _as_int(zc)
            if zi is not None:
                zc_out = zi
            elif isinstance(zc, str):
                zc_out = zc
            else:
                violations.append("%s:zc:须为整数或字符串" % tag)
                zc_out = None

        if violations:
            # 快速失败：任一违规即整批拒绝（不产出半成品条目）
            return None, violations

        entries.append({
            "entry_id": str(eid).strip(),
            "course_name": kcmc.strip() if isinstance(kcmc, str) else kcmc,
            "course_code": _as_str(it.get("kcdm")),
            "course_type": _as_str(it.get("kclx")),
            "weekday": xqj,
            "period_start": ksj,
            "period_end": jsj,
            "week_no": zc_out,
            "date": nyr.strip() if isinstance(nyr, str) else nyr,
            "date_short": _as_str(it.get("skrq")),
            "location": _as_str(it.get("jsmc")),
            "teacher_id": _as_str(it.get("jsbh")),
            "academic_year": _as_str(it.get("xn")),
            "semester": _as_str(it.get("xq")),
            "source_id": SOURCE_COURSE_API,
        })

    return entries, []
