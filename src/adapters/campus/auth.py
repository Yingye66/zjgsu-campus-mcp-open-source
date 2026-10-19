# -*- coding: utf-8 -*-
"""登录流程（认证适配器）。

复现已核实的 A 路径（见代码链报告-2026-10-13.md）：
1. POST https://myapp.zjgsu.edu.cn/api/v1/noticeIntegration/mobile/token
   参数 username/password/clientId/redirectUrl/mobileBT/source；mobileBT=DES+Base64(用户名)
2. POST .../mobile/getUserType（头 zjgsuToken=<access_token>）

由本机凭据层（src/auth/login_cli.py）在交互式终端调用；认证初始化与 MCP 请求
协议分离——MCP 工具永远不会接触密码。
"""
from __future__ import annotations

from .classify import (classify_login, classify_usertype, decide_after_login,
                       decide_after_usertype)
from .des import ecode

BASE = "https://myapp.zjgsu.edu.cn"
LOGIN_PATH = "/api/v1/noticeIntegration/mobile/token"
USERTYPE_PATH = "/api/v1/noticeIntegration/mobile/getUserType"


def perform_login(constants: dict, transport, username: str, password: str,
                  java_bin: str) -> dict:
    """执行两步登录。

    返回（不包含任何凭据出参除 token 外；token 仅返回给调用方进程内存/私有存储）：
      {
        'outcome': 'ok' | 'auth_required' | 'auth_failed' | 'network_error' |
                   'http_error' | 'service_error' | 'parser_mismatch' | 'missing_code',
        'token': str | None,       # 登录第一步的 access_token（后续请求头 zjgsuToken）
        'user_type': str | None,   # '2' = 学生（课表路径适用）
        'diag': str,               # 安全诊断文本（可直接对外）
      }
    """
    mobile_bt = ecode(java_bin, constants["des_key_b64"], username)
    r1 = transport.post_form(BASE + LOGIN_PATH, form={
        "username": username,
        "password": password,
        "clientId": constants["client_id"],
        "redirectUrl": BASE + "/home/index",
        "mobileBT": mobile_bt,
        "source": "mobile",
    })
    outcome1, token, diag1 = classify_login(r1.get("kind"), r1.get("status", 0), r1.get("text", ""))
    if not decide_after_login(outcome1):
        return {"outcome": outcome1, "token": None, "user_type": None, "diag": diag1}

    r2 = transport.post_form(BASE + USERTYPE_PATH, headers={"zjgsuToken": token})
    outcome2, user_type, diag2 = classify_usertype(r2.get("kind"), r2.get("status", 0), r2.get("text", ""))
    if not decide_after_usertype(outcome2, user_type):
        # 登录成功但身份未取得/非学生：保留 token，由调用方决定是否落会话
        return {"outcome": outcome2, "token": token, "user_type": user_type,
                "diag": "%s（登录已成功；用户类型未取得有效值）" % diag2}

    return {"outcome": "ok", "token": token, "user_type": user_type,
            "diag": "%s；%s" % (diag1, diag2)}
