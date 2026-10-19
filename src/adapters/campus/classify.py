# -*- coding: utf-8 -*-
"""响应分类器（从 validation/probe/timetable_probe.py v5 原样提取，语义不变）。

来源与连续性：
- 探针 v5 的分类器已通过 31 项合成样本回归与维护 Agent 的独立复验（13:10，
  见 validation/probe/本地检查记录.md §八）；本模块是该分类器的正式化提取。
- R09 订正：mobile/token 业务成功码为字符串 "200"（依据 BasicPushStatus.SUCCESS_CODE
  定义体；LoginActivity 引用该常量）。不同时接受 0/200，不凭 token 放行。
- R11 订正：studentCourse 采用已验证成功码白名单 '0'；未知业务码 → unknown_code，
  不采用为成功、不建立空快照、不更新最后成功时间、不映射为登录失效。
- getUserType 端点判据保持字面 '0'（端点独立）。
- 分类只做客户端判据；不代表服务端语义（如 98/99 的含义未经验证）。

适配器不复制原 App 的宽松分支（如课表端点对未知码的宽松采用）——见交接文档
第 6.5 节「保留登录业务 200、身份查询业务 0/学生门槛、课表业务 0 白名单与未知码拒绝」。
"""
from __future__ import annotations

import json

LOGIN_SUCCESS_CODE = "200"  # mobile/token 业务成功码（字符串；R09 订正，已追到常量定义）
COURSE_SUCCESS_CODES = ("0",)  # studentCourse 已验证成功码白名单（2026-10-13 10:28 实测回包）


def classify_login(kind: str, status: int, text: str) -> tuple[str, str | None, str]:
    """登录（mobile/token）响应分类 -> (outcome, token|None, diag)

    成功判据（仅本端点）：HTTP 成功 + 有效响应结构 + 业务 code=200 + 非空字符串 token。
    """
    if kind == "network_error":
        return ("network_error", None, "网络错误（未取回响应）")
    if status != 200:
        return ("http_error", None, "HTTP %s" % status)
    try:
        d = json.loads(text)
    except Exception:
        return ("parser_mismatch", None, "响应不是 JSON")
    if not isinstance(d, dict):
        return ("parser_mismatch", None, "顶层不是对象")
    t1 = d.get("data")
    if not isinstance(t1, dict):
        return ("service_error", None, "无 data 对象")
    code = t1.get("code")
    code_s = None if code is None else str(code)  # 数值/字符串统一规范化（如 200 / "200"）
    if code_s is None or code_s == "":
        return ("missing_code", None, "缺少业务 code 判据——不放行")
    cand = t1.get("access_token")
    has_token = isinstance(cand, str) and len(cand) > 0
    if code_s == LOGIN_SUCCESS_CODE and has_token:
        return ("ok", cand, "code=%s；access_token 已获取（不显示、不落盘）" % code_s)
    if code_s == LOGIN_SUCCESS_CODE and not has_token:
        return ("service_error", None, "成功码 %s，但 token 字段缺失/为空/非字符串——不放行" % code_s)
    if code_s == "40010":
        return ("auth_required", None, "需先修改密码（code=40010）")
    extra = "；响应含 token 字段但未采用" if has_token else ""
    return ("auth_failed", None, "code=%s%s（登录第一步业务失败）" % (code_s, extra))


def decide_after_login(outcome: str) -> bool:
    """仅明确成功才允许继续依赖请求。"""
    return outcome == "ok"


def classify_usertype(kind: str, status: int, text: str) -> tuple[str, str | None, str]:
    """用户类型响应分类 -> (outcome, user_type|None, diag)"""
    if kind == "network_error":
        return ("network_error", None, "网络错误（未取回响应）")
    if status != 200:
        return ("http_error", None, "HTTP %s" % status)
    try:
        d = json.loads(text)
    except Exception:
        return ("parser_mismatch", None, "响应不是 JSON")
    if not isinstance(d, dict):
        return ("parser_mismatch", None, "顶层不是对象")
    t2 = d.get("data")
    if not isinstance(t2, dict):
        return ("service_error", None, "无 data 对象")
    code = t2.get("code")
    code_s = None if code is None else str(code)
    ut = t2.get("userType")
    ut_s = None if ut is None else str(ut)
    if code_s == "0" and ut_s:
        return ("ok", ut_s, "code=0；userType=%s" % ut_s)
    return ("business_error", None, "code=%s（未取得有效 userType）" % code_s)


def decide_after_usertype(outcome: str, user_type: str | None) -> bool:
    """仅「成功取得学生身份」才请求学生课表。"""
    return outcome == "ok" and user_type == "2"


def classify_course(kind: str, status: int, text: str) -> tuple[str, list | None, str]:
    """课表响应分类 -> (outcome, items|None, diag)

    R11 判据：studentCourse 采用已验证成功码白名单（'0'；允许整数 0 规范化）；
    未知业务码 → unknown_code（不采用为成功、不建立空快照、不更新最后成功时间、
    不映射为登录失效）；98/99 → business_error；HTTP/JSON/data/code/list/元素类型逐层校验。
    """
    if kind == "network_error":
        return ("network_error", None, "网络错误（未取回响应）")
    if status != 200:
        return ("http_error", None, "HTTP %s" % status)
    try:
        d = json.loads(text)
    except Exception:
        return ("parser_mismatch", None, "响应不是 JSON")
    if not isinstance(d, dict):
        return ("parser_mismatch", None, "顶层不是对象")
    data3 = d.get("data")
    if not isinstance(data3, dict):
        return ("service_error", None, "无 data 对象")
    code = data3.get("code")
    code_s = None if code is None else str(code)
    if code_s is None or code_s == "":
        return ("missing_code", None, "缺少 code 判据——不放行")
    if code_s in ("99", "98"):
        return ("business_error", None,
                "code=%s（疑似会话/认证问题；语义未验证，不直接映射 auth_required）" % code_s)
    if code_s not in COURSE_SUCCESS_CODES:
        return ("unknown_code", None,
                "未识别的业务码 code=%s——不采用为成功结果；不建立空快照、不更新最后成功时间、"
                "不映射为登录失效（新成功码须有该端点新证据）" % code_s)
    lst = data3.get("list")
    if not isinstance(lst, list):
        return ("parser_mismatch", None, "list 不是数组——不折叠为空列表")
    for it in lst:
        if not isinstance(it, dict):
            return ("parser_mismatch", None, "list 元素不是对象")
    if len(lst) == 0:
        return ("ok_empty", [], "已知成功码 %s 的合法空列表（该查询范围内 0 条）" % code_s)
    return ("ok", lst, "%d 条记录（成功码 %s）" % (len(lst), code_s))
