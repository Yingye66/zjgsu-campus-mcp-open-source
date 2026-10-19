# -*- coding: utf-8 -*-
"""会话失效时的自动恢复（F29）：凭据层 → 自动重登（一次）→ 更新凭据状态。

设计要点（对齐 validation/review-auto-relogin-2026-10-19 的复现口径）：
- 触发方：mcp_server 包装器在工具结果为 unavailable 且含 auth_required（统一认证域）
  时调用；独立凭据域（体测 token 等，issue.auth_domain="independent"）不触发；
- 并发协调：锁外先读凭据（并发请求可并行进入），锁内重新核验版本/状态；
  同一凭据版本成功后，同批请求在复用窗口（REUSE_WINDOW_S）内直接复用，不重复登录；
- 失败熔断：认证被拒 → 先建立内存熔断（同版本后续调用一律拒绝），再尽力持久化
  （磁盘写失败也受控、不向上抛、不会二次撞登录）；网络/环境失败不熔断；
- 版本：凭据 version 为保存时生成的 uuid4——同秒等长改密可区分；重新保存即新版本，
  熔断随之解除；
- 账号绑定：自动恢复只服务于「本次查询所属账号域」——凭据账号与当前活动账号不一致时
  拒绝（account_mismatch），不登录另一账号、不切换 active、不重放为其查询；
- 全程不打印凭据；诊断文本为安全文本（适配器 diag 口径）。
"""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from ..auth import auto_login
from ..auth import credential_store as C
from ..auth.auto_login import login_and_save_session, session_generation
from ..auth.session import account_scope_id, get_active_file, load_session

_LOCK = threading.Lock()

# 同一凭据版本重登成功后的并发复用窗口（秒）：
# 覆盖「多个工具同时失效」的同批请求；超出窗口视为新的失效批次，允许再次重登。
REUSE_WINDOW_S = 120.0

# 进程内协调状态（不含凭据内容）：
# cred_fp = 凭据版本（credential_store 的 version），outcome = ok|failed_auth，
# session_fp = 登录成功后会话 token 的指纹——复用前必须与当前会话一致，
# 会话被替换/破坏（如另一个域失效、手工改会话）时不得复用，须重新登录。
_LAST = {"cred_fp": None, "outcome": None, "at": None, "session_fp": None}


def _session_fp(data_dir: Path, account: str | None) -> str | None:
    """当前会话 token 的短指纹（只用于比对，不含 token 明文）。"""
    if not account:
        return None
    try:
        sess = load_session(data_dir, account) or {}
        token = sess.get("zjgsu_token") or ""
        if not token:
            return None
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return None


def _mono() -> float:
    return time.monotonic()


def _client_id(data_dir: Path) -> str | None:
    """客户端常量（用于凭据→账号域推导）；模块属性访问以便隔离测试替换环境。"""
    try:
        constants, _ = auto_login.check_environment(data_dir)
        if isinstance(constants, dict):
            return constants.get("client_id")
    except Exception:
        pass
    return None


def _target_account(data_dir: Path, cred: dict) -> tuple[str | None, str | None]:
    """由凭据推导目标账号域；无法推导返回 (None, reason)。"""
    cid = _client_id(data_dir)
    if not cid:
        return None, "environment"
    try:
        return account_scope_id(cred["username"], cid), None
    except Exception:
        return None, "environment"


def _active_account(data_dir: Path) -> str | None:
    """磁盘真实生效账号域（不受请求上下文绑定影响）。"""
    try:
        return get_active_file(data_dir)
    except Exception:
        return None


def attempt_auto_relogin(data_dir: Path,
                         expected_account: str | None = None) -> dict:
    """尝试用本机凭据层自动重登；返回 {outcome: ok|failed|skipped, ...}。

    - skipped：凭据层未启用/超期/熔断/账号不匹配/环境不可用（调用方按 reason 决定提示）；
    - ok：登录与会话保存成功（凭据层已记录 last_login_ok_at；reused=True 表示同批复用）；
    - failed：认证被拒（已内存+持久化熔断）或网络/环境/会话核对问题（未熔断）。
    """
    data_dir = Path(data_dir)

    # —— 锁外快速判定（并发请求可先行读取凭据，不被持锁登录阻塞）——
    cred = C.load_credentials(data_dir)
    ev = C.evaluate(cred)
    if not ev.get("usable"):
        return {"outcome": "skipped", "reason": ev.get("reason")}

    expected = (expected_account if expected_account is not None
                else _active_account(data_dir))
    target, why = _target_account(data_dir, cred)
    if expected:
        if target is None:
            return {"outcome": "skipped", "reason": why or "environment"}
        if target != expected:
            return {"outcome": "skipped", "reason": "account_mismatch",
                    "credential_account": target}

    with _LOCK:
        # —— 锁内重新核验：状态/版本/账号（同一批并发只允许一次登录）——
        cred2 = C.load_credentials(data_dir)
        ev2 = C.evaluate(cred2)
        if not ev2.get("usable"):
            return {"outcome": "skipped", "reason": ev2.get("reason")}

        version = cred2.get("version")
        fp_now = _session_fp(data_dir, expected)
        if _LAST["cred_fp"] == version:
            if _LAST["outcome"] == "ok" and _LAST.get("session_fp") == fp_now and \
                    (_mono() - (_LAST["at"] or 0.0)) < REUSE_WINDOW_S:
                return {"outcome": "ok", "reused": True,
                        "diag": "同批请求已由其它调用完成自动重登"}
            if _LAST["outcome"] == "failed_auth":
                return {"outcome": "failed", "auth_failed": True,
                        "diag": "同版本凭据刚自动登录失败（内存熔断，本次不再尝试）"}

        # 账号绑定锁内复核（防并发切号竞态）
        expected2 = expected if expected is not None else _active_account(data_dir)
        target2, why2 = _target_account(data_dir, cred2)
        if expected2:
            if target2 is None:
                return {"outcome": "skipped", "reason": why2 or "environment"}
            if target2 != expected2:
                return {"outcome": "skipped", "reason": "account_mismatch",
                        "credential_account": target2}

        # F29-R1：启动登录时固定会话代次快照；发布时若已变化则整批放弃
        generation = session_generation(data_dir, expected2)
        try:
            # 位置参数：兼容隔离测试/审查脚本对登录编排的替换签名
            result = login_and_save_session(
                data_dir, cred2["username"], cred2["password"], expected2, generation)
        except Exception as e:  # 兜底：登录桥异常不得打断工具调用
            result = {"outcome": "network",
                      "diag": "%s: %s" % (type(e).__name__, e)}

        outcome = result.get("outcome")
        if outcome == "ok":
            try:
                # F29-R2：绑定启动登录时的凭据版本——旧结果不得修改新凭据状态
                C.mark_login_ok(data_dir, version)
            except Exception:
                pass  # 状态持久化失败不影响本次已建立的会话
            _LAST.update(cred_fp=version, outcome="ok", at=_mono(),
                         session_fp=_session_fp(data_dir, expected2))
            return {"outcome": "ok", "diag": result.get("diag", ""),
                    "jwweb_ok": bool(result.get("jwweb_ok")),
                    "session_version": result.get("session_version")}
        if outcome in ("account_changed", "session_changed"):
            # 用户在恢复期间切号/重新登录：本次旧恢复整批放弃（未覆盖任何东西）
            return {"outcome": "skipped", "reason": "context_changed",
                    "detail": outcome, "diag": result.get("diag", "")}
        if outcome == "account_mismatch":
            return {"outcome": "skipped", "reason": "account_mismatch",
                    "diag": result.get("diag", "")}
        if outcome == "auth_failed":
            # 先建立内存熔断（持久化失败也不得再撞登录），再尽力持久化
            _LAST.update(cred_fp=version, outcome="failed_auth", at=_mono())
            try:
                C.mark_login_failed(data_dir, result.get("diag", ""), version)
            except Exception:
                pass  # 磁盘写失败受控：内存熔断已生效
            return {"outcome": "failed", "auth_failed": True,
                    "diag": result.get("diag", "")}
        # network / environment / session_mismatch：不熔断
        return {"outcome": "failed", "auth_failed": False,
                "diag": result.get("diag", "")}


def credential_issue_hint(data_dir: Path, reason: str | None = None) -> dict | None:
    """供调用方在自动重登失败/被拒后追加提醒 issue（None 表示无需追加）。

    reason 可传 attempt 结果的 reason（更精确：如 account_mismatch）；
    未传时按凭据文件当前状态推断。"""
    if reason == "account_mismatch":
        return {"hint": "本机已保存的凭据属于另一个账号域；自动恢复不会切换账号"
                        "或读取他人缓存。如确需切换账号，请在终端显式运行 "
                        "python -m src.auth.login_cli init。"}
    if reason == "context_changed":
        return {"hint": "恢复期间账号或会话已切换，本次自动恢复已放弃"
                        "（没有覆盖你的选择）；请重新发起一次查询。"}
    if reason == "environment":
        return {"hint": "本机登录环境不可用（客户端常量/Java）；与凭据无关，"
                        "请检查运行环境后重试。"}

    cred = C.load_credentials(data_dir)
    ev = C.evaluate(cred)
    if reason == C.REASON_EXPIRED or ev.get("reason") == C.REASON_EXPIRED:
        return {"hint": "已保存凭据已超期（可能已改密）；请运行 "
                        "python -m src.auth.login_cli cred-save 更新。"}
    if reason == C.REASON_NEEDS_UPDATE or ev.get("reason") == C.REASON_NEEDS_UPDATE:
        return {"hint": "上次自动登录失败（%s）；请确认密码后运行 "
                        "python -m src.auth.login_cli cred-save 更新。"
                        % ((cred or {}).get("last_fail_diag") or "原因未知")}
    if ev.get("reason") == C.REASON_MISSING:
        return {"hint": "本机未保存凭据（或凭据文件损坏）；如需自动恢复请运行 "
                        "python -m src.auth.login_cli cred-save（或 init --save-cred），"
                        "或直接重新运行 init。"}
    return {"hint": "自动登录未成功（%s）；可重试或运行 cred-save 更新凭据。"
                    % ev.get("reason")}
