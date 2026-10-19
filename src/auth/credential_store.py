# -*- coding: utf-8 -*-
"""本机凭据存储：统一认证账号密码 + 时效/版本/状态管理（F29）。

边界与约定（与会话文件同等安全边界，见 src/auth/session.py 已知限制）：
- 文件：data/credentials.json（data/ 已被 .gitignore 排除；不提交、不外传）；
- 仅存「统一身份认证」账号密码；选座 Cookie / 体测 token 等独立凭据仍走各自导入命令，
  不归本存储管理；
- 密码为明文 JSON（本机私有目录 + .gitignore；未做 Windows ACL 收紧）——
  与会话文件（含 token）同级别边界，如实标注不做更高承诺；
- 原子写入（写 tmp → os.replace），不打印、不写日志、不进 MCP 工具输出；
- 版本：每次保存生成 `version`（uuid4）——用于自动恢复的并发去重与失败熔断绑定，
  不依赖秒级时间戳或密码长度（同秒等长改密必须可区分）；
- 严格元数据校验：saved_at 必须为带时区的 ISO 8601，ttl_days 必须为有限正数；
  缺失/损坏/非法一律按「不可用」处理（load 返回 None），给更新指引，不自动使用；
- 时效语义：saved_at + ttl_days（默认 180 天）后视为「可能已改密」，
  自动登录拒绝使用并提醒更新——防止用旧密码反复撞登录（连续失败可能锁号）；
- 失败熔断：自动登录被拒一次即 needs_update=True；持久化失败时调用方内存熔断仍然生效
  （mark_* 不向上抛异常，返回是否持久化成功）。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .file_lock import authentication_lock

FILENAME = "credentials.json"
SCHEMA = 1
DEFAULT_TTL_DAYS = 180

# 保存、清除与状态回写共用数据目录内的跨进程锁；版本核对与写入不可分步。

# 中国无夏令时：固定 +08:00（与 login_cli/session 既有口径一致）
TZ = timezone(timedelta(hours=8))

# evaluate() 的 reason 词表（对外安全文本，不含秘密）
REASON_OK = "ok"
REASON_MISSING = "credentials_missing"      # 未保存/损坏/元数据非法（load 层折叠为缺失）
REASON_EXPIRED = "credentials_expired"      # 超过 TTL：可能已改密，拒绝自动使用
REASON_NEEDS_UPDATE = "credentials_needs_update"  # 上次自动登录失败，已熔断


class CredentialWriteError(Exception):
    """凭据写入失败（磁盘不可写/被占用等）。旧文件保持不变，调用方不得报告成功。"""


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILENAME


def _now_s() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _parse_ts(iso_ts: object) -> datetime | None:
    """严格解析：必须是带时区的 ISO 8601 字符串，否则 None（naive 视为非法）。"""
    if not isinstance(iso_ts, str) or not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        return None  # 无时区：时效不可信，按损坏处理
    return ts


def _valid_ttl(ttl) -> bool:
    """有限正数（排除 bool / NaN / inf / 字符串）。"""
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        return False
    return math.isfinite(float(ttl)) and float(ttl) > 0


def _validate(record: object) -> dict | None:
    """完整结构校验；任一不合法 → None（按不可用处理，不猜测修复）。"""
    if not isinstance(record, dict):
        return None
    if record.get("schema") != SCHEMA:
        return None
    if not isinstance(record.get("username"), str) or not record.get("username"):
        return None
    if not isinstance(record.get("password"), str) or not record.get("password"):
        return None
    if not isinstance(record.get("version"), str) or not record.get("version"):
        return None
    if _parse_ts(record.get("saved_at")) is None:
        return None
    if not _valid_ttl(record.get("ttl_days")):
        return None
    return record


def save_credentials(data_dir: Path, username: str, password: str,
                     ttl_days: int = DEFAULT_TTL_DAYS) -> Path:
    """写入/覆盖凭据（原子写 + 新版本号）；成功后重置失败熔断状态。"""
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise ValueError("username/password 不能为空")
    try:
        ttl_days = int(ttl_days)
    except (TypeError, ValueError):
        ttl_days = DEFAULT_TTL_DAYS
    if ttl_days <= 0:
        raise ValueError("ttl_days 必须为正整数（天）")
    record = {
        "schema": SCHEMA,
        "username": username,
        "password": password,
        # F29：显式版本号——同秒、等长改密也可区分（并发去重/熔断绑定用）
        "version": uuid.uuid4().hex,
        "saved_at": _now_s(),
        "ttl_days": ttl_days,
        "last_login_ok_at": None,
        "last_fail_at": None,
        "fail_count": 0,
        "last_fail_diag": None,
        "needs_update": False,
    }
    # F29-R3：写入失败必须显式区分——不得返回成功让调用方打印“已保存”
    with authentication_lock(data_dir):
        if not _write_atomic(data_dir, record):
            raise CredentialWriteError(
                "凭据写入失败（磁盘不可写或被占用）；旧凭据未被修改")
    return _path(data_dir)


def load_credentials(data_dir: Path) -> dict | None:
    """读取凭据；缺失/损坏/元数据非法 → None（调用方按不可用处理）。"""
    path = _path(data_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _validate(data)


def clear_credentials(data_dir: Path) -> bool:
    with authentication_lock(data_dir):
        path = _path(data_dir)
        if path.exists():
            path.unlink()
            return True
        return False


def evaluate(cred: dict | None, now: datetime | None = None) -> dict:
    """判定凭据可用性（只返回状态元数据，绝不含密码/用户名原文；永不抛异常）。

    usable=True 表示「允许自动登录尝试」；其余 reason 均拒绝。"""
    try:
        now = now or datetime.now(TZ)
        if not isinstance(cred, dict):
            return {"present": False, "usable": False, "reason": REASON_MISSING,
                    "expired": False, "needs_update": False, "age_days": None}
        saved_at = _parse_ts(cred.get("saved_at"))
        ttl_days = cred.get("ttl_days")
        if saved_at is None or not _valid_ttl(ttl_days):
            # 元数据缺失/非法：时效不可信 → 拒绝（按损坏处理，不猜默认值）
            return {"present": False, "usable": False, "reason": REASON_MISSING,
                    "expired": False, "needs_update": False, "age_days": None}
        age_days = max(0.0, (now - saved_at).total_seconds() / 86400.0)
        expired = age_days > float(ttl_days)
        needs_update = bool(cred.get("needs_update"))
        if expired:
            reason = REASON_EXPIRED
        elif needs_update:
            reason = REASON_NEEDS_UPDATE
        else:
            reason = REASON_OK
        return {"present": True, "usable": reason == REASON_OK, "reason": reason,
                "expired": expired, "needs_update": needs_update,
                "age_days": round(age_days, 2)}
    except Exception:
        # 防御：任何内部异常都按不可用返回（不得打断 CLI/工具）
        return {"present": False, "usable": False, "reason": REASON_MISSING,
                "expired": False, "needs_update": False, "age_days": None}


def mark_login_ok(data_dir: Path, expected_version: str | None = None) -> bool:
    """记录成功并解除熔断；持久化失败不抛异常（返回 False）。

    F29-R2：expected_version 为启动登录时的凭据版本——若期间用户重新保存/清除，
    版本不一致则本次旧结果无效（不修改新凭据状态）。"""
    with authentication_lock(data_dir):
        cred = load_credentials(data_dir)
        if cred is None:
            return False
        if expected_version is not None and cred.get("version") != expected_version:
            return False
        cred["last_login_ok_at"] = _now_s()
        cred["needs_update"] = False
        cred["fail_count"] = 0
        cred["last_fail_at"] = None
        cred["last_fail_diag"] = None
        return _safe_write(data_dir, cred)


def mark_login_failed(data_dir: Path, diag: str,
                      expected_version: str | None = None) -> bool:
    """登录失败熔断：needs_update=True；持久化失败不抛异常（返回 False）。

    F29-R2：expected_version 不一致（期间重新保存过凭据）时不做任何写入——
    旧密码的失败不得熔断新密码。调用方必须先建立内存熔断，磁盘写失败时
    本次进程内也不得再撞登录。"""
    with authentication_lock(data_dir):
        cred = load_credentials(data_dir)
        if cred is None:
            return False
        if expected_version is not None and cred.get("version") != expected_version:
            return False
        cred["last_fail_at"] = _now_s()
        cred["fail_count"] = int(cred.get("fail_count") or 0) + 1
        # diag 来自登录适配器的安全诊断文本（不含凭据）；仍做长度截断防御
        cred["last_fail_diag"] = str(diag or "")[:200]
        cred["needs_update"] = True
        return _safe_write(data_dir, cred)


def _safe_write(data_dir: Path, record: dict) -> bool:
    """状态持久化的最后防线：任何异常都不向上传播（调用方已有内存熔断兜底）。"""
    try:
        return _write_atomic(data_dir, record)
    except Exception:
        return False


def _write_atomic(data_dir: Path, record: dict) -> bool:
    """原子写（tmp + os.replace）；任何 IO 失败返回 False，不向上抛。"""
    path = _path(data_dir)
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cred-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        tmp = None
        try:
            os.chmod(path, 0o600)  # POSIX 生效；Windows 忽略（与会话文件同边界）
        except OSError:
            pass
        return True
    except OSError:
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def status_for_display(data_dir: Path, now: datetime | None = None) -> dict:
    """CLI/工具展示用状态（用户名掩码，绝不含密码）。"""
    cred = load_credentials(data_dir)
    ev = evaluate(cred, now=now)
    out = dict(ev)
    if cred is None:
        out.update({"username_masked": None, "saved_at": None, "ttl_days": None})
        return out
    username = cred.get("username") or ""
    masked = (username[:4] + "****" + username[-2:]) if len(username) > 4 else "****"
    out.update({
        "username_masked": masked,
        "saved_at": cred.get("saved_at"),
        "ttl_days": cred.get("ttl_days"),
        "last_login_ok_at": cred.get("last_login_ok_at"),
        "last_fail_at": cred.get("last_fail_at"),
        "fail_count": cred.get("fail_count"),
        "last_fail_diag": cred.get("last_fail_diag"),
    })
    return out
