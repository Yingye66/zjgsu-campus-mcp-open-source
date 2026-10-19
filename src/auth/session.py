# -*- coding: utf-8 -*-
"""本机凭据边界：会话存取与账号域标识。

约定（对应交接文档第 7 节）：
- 会话由本机凭据层管理：登录初始化（login_cli）写入 data/sessions/<account_scope_id>.json；
  MCP 工具只读取按 module 选择的授权上下文，不接受客户端传入的账号参数；
- account_scope_id = 'acct-' + sha256(用户名|clientId)[:16]：不可逆、稳定、跨账号不共享；
  原始用户名与密码不写入会话文件、不进工具输出；
- data/active_session.json 记录当前生效账号域（登录时更新；登出时清除）。
  「切换账号」= 重新登录；上一身份的缓存与游标随之不可读（服务层强制校验）。
- 已知限制：会话文件为明文 JSON（本机用户目录 + .gitignore 保护；未做 Windows ACL 收紧）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SESSIONS_DIRNAME = "sessions"
ACTIVE_FILE = "active_session.json"


def account_scope_id(username: str, client_id: str) -> str:
    raw = ("%s|%s" % (username, client_id)).encode("utf-8")
    return "acct-" + hashlib.sha256(raw).hexdigest()[:16]


def _sessions_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / SESSIONS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(data_dir: Path, account: str, session: dict) -> Path:
    path = _sessions_dir(data_dir) / ("%s.json" % account)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_session(data_dir: Path, account: str) -> dict | None:
    path = _sessions_dir(data_dir) / ("%s.json" % account)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("zjgsu_token"):
        return None
    return data


def delete_session(data_dir: Path, account: str) -> bool:
    path = _sessions_dir(data_dir) / ("%s.json" % account)
    if path.exists():
        path.unlink()
        return True
    return False


def set_active(data_dir: Path, account: str) -> None:
    (Path(data_dir) / ACTIVE_FILE).write_text(
        json.dumps({"account_scope_id": account}, ensure_ascii=False, indent=2), encoding="utf-8")


def get_active(data_dir: Path) -> str | None:
    path = Path(data_dir) / ACTIVE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("account_scope_id")
    except Exception:
        return None


def clear_active(data_dir: Path) -> None:
    path = Path(data_dir) / ACTIVE_FILE
    if path.exists():
        path.unlink()
