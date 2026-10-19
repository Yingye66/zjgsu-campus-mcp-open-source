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

import contextvars
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from .file_lock import authentication_lock

SESSIONS_DIRNAME = "sessions"
ACTIVE_FILE = "active_session.json"

# F29-R1：请求级账号绑定——自动恢复期间，工具内部读取 active 时必须使用
# 「首次查询开始前」固定的账号，避免查询中切号导致跨账号读取/重放。
# 绑定优先于磁盘真实值；恢复自身的发布校验必须用 get_active_file（真实读取）。
_BOUND_ACCOUNT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "zjgsu_bound_account", default=None)


def bind_account(account: str | None):
    """在当前上下文绑定账号域（返回 token，供 reset_account 恢复）。"""
    return _BOUND_ACCOUNT.set(account)


def reset_account(token) -> None:
    _BOUND_ACCOUNT.reset(token)


def bound_account() -> str | None:
    """当前上下文绑定的账号域；未绑定返回 None。"""
    return _BOUND_ACCOUNT.get()


def account_scope_id(username: str, client_id: str) -> str:
    raw = ("%s|%s" % (username, client_id)).encode("utf-8")
    return "acct-" + hashlib.sha256(raw).hexdigest()[:16]


def _sessions_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / SESSIONS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(data_dir: Path, account: str, session: dict) -> Path:
    with authentication_lock(data_dir):
        path = _sessions_dir(data_dir) / ("%s.json" % account)
        record = dict(session, session_version=uuid.uuid4().hex)
        _write_json(path, record)
        return path


def _write_json(path: Path, record: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_session(data_dir: Path, account: str) -> dict | None:
    with authentication_lock(data_dir):
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
    with authentication_lock(data_dir):
        path = _sessions_dir(data_dir) / ("%s.json" % account)
        if path.exists():
            path.unlink()
            return True
        return False


def set_active(data_dir: Path, account: str) -> None:
    with authentication_lock(data_dir):
        _write_json(Path(data_dir) / ACTIVE_FILE, {"account_scope_id": account})


def get_active_file(data_dir: Path) -> str | None:
    """磁盘上的真实生效账号域（不受上下文绑定影响；发布/校验必须用它）。"""
    with authentication_lock(data_dir):
        path = Path(data_dir) / ACTIVE_FILE
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("account_scope_id")
        except Exception:
            return None


def get_active(data_dir: Path) -> str | None:
    """当前生效账号域：请求上下文有绑定则用绑定值（F29-R1），否则读磁盘。"""
    bound = _BOUND_ACCOUNT.get()
    if bound is not None:
        return bound
    return get_active_file(data_dir)


def clear_active(data_dir: Path) -> None:
    with authentication_lock(data_dir):
        path = Path(data_dir) / ACTIVE_FILE
        if path.exists():
            path.unlink()
