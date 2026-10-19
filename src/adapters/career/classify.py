# -*- coding: utf-8 -*-
"""就业平台响应分类（纯函数，供适配器与测试共用）。

依据：validation/career/取证记录.md（2026-10-13 公开源实测）。
- 成功判据：HTTP 200 + JSON 对象 + 业务 code=200 + data 含 list/total/pages 结构；
- code=500「操作异常！」为参数/服务端异常（实测于非法参数组合）；
- 其余 code 一律 business_error（不映射为登录失效——列表接口匿名可读，未遇到过 403）。
"""
from __future__ import annotations

import json


def classify_events(kind: str, status: int, text: str) -> tuple[str, dict | None, str]:
    """宣讲会列表响应分类 -> (outcome, data|None, diag)

    outcome ∈ {ok, ok_empty, network_error, http_error, parser_mismatch,
               service_error, missing_code, business_error}
    data 为响应中 data 对象（含 list/total/pages 等），仅在结构有效时返回。
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
    code = d.get("code")
    code_s = None if code is None else str(code)
    if code_s is None or code_s == "":
        return ("missing_code", None, "缺少业务 code 判据——不放行")
    if code_s != "200":
        # code=500/403 等：一律具名业务失败；不映射为登录失效（列表接口匿名可读）
        return ("business_error", None, "业务失败（code=%s）" % code_s)
    data = d.get("data")
    if not isinstance(data, dict):
        return ("service_error", None, "无 data 对象")
    lst = data.get("list")
    total = data.get("total")
    pages = data.get("pages")
    if not isinstance(lst, list):
        return ("parser_mismatch", None, "list 不是数组——不折叠为空列表")
    for it in lst:
        if not isinstance(it, dict):
            return ("parser_mismatch", None, "list 元素不是对象")
    if not (isinstance(total, (int, str)) and str(total).isdigit()):
        return ("parser_mismatch", None, "total 不是数字——完整性判据缺失")
    if not isinstance(pages, (int, str)) or not str(pages).isdigit():
        return ("parser_mismatch", None, "pages 不是数字——完整性判据缺失")
    # R14（交接 2.11）：空列表与 total>0 矛盾——不得成为成功空快照
    if len(lst) == 0 and int(str(total)) > 0:
        return ("parser_mismatch", None,
                "list 为空但 total=%s 矛盾——不作为成功空结果" % total)
    if len(lst) == 0:
        return ("ok_empty", data, "合法空列表（total=%s）" % total)
    return ("ok", data, "%d 条（total=%s，pages=%s）" % (len(lst), total, pages))
