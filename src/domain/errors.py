# -*- coding: utf-8 -*-
"""错误码与 issue 构造。

对应 docs/技术设计与Agent交接.md 第 8 节的「业务错误建议」清单；其中
unknown_code / schema_invalid / missing_code / business_error / service_error
沿用探针（validation/probe/timetable_probe.py v5）的分类词汇，保持连续性。

issue 的 safe_message 只允许「已审核的安全文本」——不回显服务端 message/status
原文，不包含 token、账号标识、票据或响应正文。
"""
from __future__ import annotations

# —— issue 代码（对外具名状态） ——
AUTH_REQUIRED = "auth_required"            # 本机无有效会话 / 需先运行登录初始化
NETWORK_ERROR = "network_error"            # 网络错误（未取回响应）
HTTP_ERROR = "http_error"                  # HTTP 状态异常
PARSER_MISMATCH = "parser_mismatch"        # 响应结构/字段不符合已验证契约
SERVICE_ERROR = "service_error"            # 响应层次缺 data 等
MISSING_CODE = "missing_code"              # 缺业务 code 判据——不放行
BUSINESS_ERROR = "business_error"          # 业务失败码（含 98/99；语义未验证）
UNKNOWN_CODE = "unknown_code"              # 未识别的业务码——不采用为成功
SNAPSHOT_EXPIRED = "snapshot_expired"      # 游标绑定的本地快照已不可用
INVALID_ARGUMENTS = "invalid_arguments"    # 参数不合法
COMPLETENESS_UNPROVEN = "completeness_unproven"  # 完整性待证（信息性；非失败）
UNSUPPORTED = "unsupported"                # 当前身份/条件下该模块不可用

# 探针分类 outcome → issue 代码（供 service 统一映射）
OUTCOME_TO_CODE = {
    "network_error": NETWORK_ERROR,
    "http_error": HTTP_ERROR,
    "parser_mismatch": PARSER_MISMATCH,
    "service_error": SERVICE_ERROR,
    "missing_code": MISSING_CODE,
    "business_error": BUSINESS_ERROR,
    "unknown_code": UNKNOWN_CODE,
    "schema_invalid": PARSER_MISMATCH,
}


def issue(code: str, retryable: bool, safe_message: str) -> dict:
    """构造统一 issue 结构（与第 8 节包装契约一致）。"""
    return {"code": code, "retryable": bool(retryable), "safe_message": str(safe_message)}
