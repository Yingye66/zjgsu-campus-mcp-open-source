# -*- coding: utf-8 -*-
"""用明文凭据执行完整登录并保存本机会话（login_cli 与自动重登共用的主体）。

从 login_cli.cmd_init 抽取的登录编排（保持同一语义）：
- 前置检查：客户端常量 + Java（DES 桥）——缺失属于环境问题，不算凭据错误；
- App 会话：perform_login → zjgsu_token 会话文件；
- 教务网页版（jwglxt）CAS 会话：同一凭据顺带建立；失败/异常不阻断已成功的 App 会话；
- 独立凭据（选座 Cookie / 体测 token）重跑时保留，不静默丢失；
- 明文密码只在本次调用栈内存在，函数返回前不持有（调用方负责 del）。

结果词表：
- outcome=ok            登录成功（jwweb_ok 指示教务会话是否顺带建立）
- outcome=auth_failed   认证被拒（密码错误/需改密等）→ 调用方应对凭据层熔断
- outcome=network       网络等非凭据因素 → 调用方不应熔断
- outcome=environment   客户端常量缺失/无 Java → 与凭据无关，不应熔断
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.campus import auth as campus_auth
from ..adapters.campus.constants import CampusConfigError, load_client_constants
from ..adapters.campus.des import find_java
from .session import (account_scope_id, get_active_file, load_session, save_session,
                      set_active)
from .file_lock import authentication_lock

TZ = timezone(timedelta(hours=8))

# Explicit CLI init omits expectations; automatic recovery can explicitly bind
# even an absent account/session, so a concurrent login is not overwritten.
_UNSET = object()

# 独立凭据（不随统一认证会话轮换）：重登时保留，避免静默丢失（与 login_cli 同表）
_PRESERVED_SESSION_KEYS = ("seat_cookie", "seat_cookie_saved_at",
                           "tzjk_token", "tzjk_student_no")


def _now_s() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _preserve_independent_credentials(session: dict, old: dict | None) -> list:
    kept = []
    if not isinstance(old, dict):
        return kept
    for key in _PRESERVED_SESSION_KEYS:
        if old.get(key) and not session.get(key):
            session[key] = old[key]
            kept.append(key)
    return kept


def check_environment(data_dir: Path) -> tuple[dict | None, str | None]:
    """登录前置检查：返回 (constants, error_diag)。constants 为 None 表示环境不可用。"""
    try:
        constants = load_client_constants(data_dir)
    except CampusConfigError as e:
        return None, "客户端常量缺失：%s" % e
    if not find_java():
        return None, "未找到可用 java（DES 加密桥需要完整 JDK）"
    return constants, None


def session_generation(data_dir: Path, account: str | None) -> str | None:
    """每次会话写入的唯一版本；旧会话回退到 created_at，无会话返回 None。"""
    if not account:
        return None
    sess = load_session(Path(data_dir), account)
    return None if not sess else sess.get("session_version", sess.get("created_at"))


def login_and_save_session(data_dir: Path, username: str, password: str,
                           expected_account: str | None | object = _UNSET,
                           expected_generation: str | None | object = _UNSET) -> dict:
    """执行 App + jwglxt 双登录并保存会话；返回结构化结果（不含凭据/token）。

    F29-R1/R2 提交协调：
    - 自动恢复显式传入账号及版本（可为None），凭据账号域必须一致；
    - 发布（写会话 + 写 active）使用与CLI/凭据管理相同的跨进程锁，先校验后写：
      若 active 已被切换（account_changed）或会话代次已变（session_changed），
      本次恢复整批放弃，绝不覆盖用户刚做的选择；
    - 显式CLI init省略期望参数；None是“没有账号/会话”的有效快照。"""
    data_dir = Path(data_dir)

    def _env_result():
        constants, env_err = check_environment(data_dir)
        if constants is None:
            return None, {"outcome": "environment", "diag": env_err or ""}
        return constants, None

    constants, env_fail = _env_result()
    if env_fail:
        return env_fail

    acct = account_scope_id(username, constants["client_id"])
    if expected_account is not _UNSET and expected_account is not None and acct != expected_account:
        return {"outcome": "account_mismatch",
                "diag": "凭据账号域与本次查询的账号域不一致；自动恢复不会切换账号"}

    from ..adapters.campus.transport import UrllibTransport
    transport = UrllibTransport()
    try:
        result = campus_auth.perform_login(constants, transport, username, password,
                                           find_java())
    except Exception as e:  # 防御：登录桥异常不向上抛（自动重登场景必须兜底）
        return {"outcome": "network", "diag": "%s: %s" % (type(e).__name__, e)}

    if result.get("outcome") != "ok":
        mapped = "auth_failed" if result.get("outcome") in ("auth_failed",
                                                            "auth_required") else "network"
        return {"outcome": mapped, "diag": "%s：%s" % (result.get("outcome"),
                                                      result.get("diag"))}

    session = {
        "schema": 1,
        "account_scope_id": acct,
        "client_id": constants["client_id"],
        "zjgsu_token": result["token"],
        "user_type": result["user_type"],
        "created_at": _now_s(),
    }

    # —— 教务网页版（jwglxt）CAS 会话：同一凭据顺带建立；失败不阻断 App 会话 ——
    jw_ok = False
    jw_diag = ""
    try:
        from ..adapters.campus.jwweb import cas_login as jwweb_cas_login
        jw = jwweb_cas_login(username, password)
        if jw.get("outcome") == "ok":
            session["jwglxt_cookies"] = jw["cookies"]
            if jw.get("app_cookies"):
                session["app_cookies"] = jw["app_cookies"]
            jw_ok = True
        else:
            jw_diag = "%s：%s" % (jw.get("outcome"), jw.get("diag"))
    except Exception as e:
        jw_diag = "%s: %s" % (type(e).__name__, e)

    # —— F29-R1：发布（校验 → 写会话 → 写 active 不可分步）——
    with authentication_lock(data_dir):
        # 先校验后写：用户若在登录期间切号/重新登录，本次旧恢复整批放弃
        current = get_active_file(data_dir)
        if expected_account is not _UNSET and current != expected_account:
            return {"outcome": "account_changed",
                    "diag": "登录期间账号已切换（%s -> %s）；本次恢复已放弃，"
                            "未覆盖新的账号选择" % (expected_account, current)}
        if expected_generation is not _UNSET and \
                session_generation(data_dir, acct) != expected_generation:
            return {"outcome": "session_changed",
                    "diag": "登录期间会话已更新；本次恢复已放弃（旧结果不覆盖新会话）"}
        old_session = load_session(data_dir, acct)
        kept = _preserve_independent_credentials(session, old_session)
        save_session(data_dir, acct, session)
        set_active(data_dir, acct)
        published = load_session(data_dir, acct)
        published_version = (published or {}).get("session_version")

    # 发布后核对（不掩盖上面的前置校验；仅防御写入异常）
    if get_active_file(data_dir) != acct:
        return {"outcome": "session_mismatch",
                "diag": "登录后活动账号域核对失败"}
    fresh = load_session(data_dir, acct)
    if not fresh or not fresh.get("zjgsu_token") or fresh.get("session_version") != published_version:
        return {"outcome": "session_mismatch", "diag": "登录后会话文件核对失败"}

    old_type = (old_session or {}).get("user_type")
    return {
        "outcome": "ok",
        "account_scope_id": acct,
        "session_version": published_version,
        "user_type": result["user_type"],
        "diag": result.get("diag") or "",
        "jwweb_ok": jw_ok,
        "jwweb_diag": jw_diag,
        "kept_independent": kept,
        "identity_changed": (old_type is not None
                             and str(old_type) != str(result["user_type"])),
        "old_user_type": old_type,
    }
