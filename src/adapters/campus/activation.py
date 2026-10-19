# -*- coding: utf-8 -*-
"""教务功能码激活状态账本（P0，默认开启，可用环境变量关闭）。

背景（2026-10-18 实测）：同一会话内，已激活过 gnmkdm 的后续 POST 可以跳过
Index GET（成绩 895→301ms、课表 761→461ms）。但「单次短时样本」不足以证明
服务端会同时记住多个功能码，也不覆盖并发、Cookie 轮换、状态丢失。

本模块只提供**有条件跳过**所需的账本，不改变请求语义：
- 键 = (host, gnmkdm, 会话指纹)；Cookie 轮换自然 miss；
- 只有 POST 成功后才标记 active；
- 910 / 空结果 / 错误由调用方触发 invalidate 与一次重试；
- **默认开启**：显式 ZJGSU_JW_ACTIVATION_MEMO=0 可关闭（排障/回滚）；
- 进程内、无磁盘、无后台线程、无保活。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

ENV_FLAG = "ZJGSU_JW_ACTIVATION_MEMO"
DEFAULT_TTL_SECONDS = 1800.0   # 保守上限，不是学校状态生命周期的证明
DEFAULT_MAX_ENTRIES = 32


def memo_enabled() -> bool:
    """默认开启；显式 ZJGSU_JW_ACTIVATION_MEMO=0 关闭（排障/回滚）。

    默认开启的依据（2026-10-18 真机只读 A/B，validation/activation-memo-2026-10-18）：
    - off 905.5ms / cold 855.1ms / hot 314.5ms（成绩，5 次中位数）；
    - hot 时 GET=0、POST=1，条目数与 off 完全一致；
    - 开关开启下 305 项全量测试通过。
    """
    raw = os.environ.get(ENV_FLAG)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "off", "no")


@dataclass(frozen=True)
class ActivationKey:
    host: str
    gnmkdm: str
    session_fp: str


def session_fingerprint(cookies) -> str:
    """对 cookie 列表做稳定指纹（只取身份相关字段，不含时间戳）。"""
    rows = []
    for c in cookies or []:
        if isinstance(c, dict):
            rows.append([c.get("domain"), c.get("path"),
                         c.get("name"), c.get("value")])
        else:
            rows.append([str(c)])
    rows.sort(key=lambda r: [str(x) for x in r])
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ActivationLedger:
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES, clock=None):
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock or time.monotonic
        self._entries: "OrderedDict[ActivationKey, float]" = OrderedDict()
        self._guard = threading.RLock()
        # 会话级锁：同一 cookie 会话的所有激活请求串行（不分功能码）。
        # 锁随进程存续、不随账本条目淘汰——淘汰条目绝不释放仍在使用的锁。
        self._session_locks: dict[str, threading.Lock] = {}

    def is_active(self, key: ActivationKey) -> bool:
        if key is None:
            return False
        with self._guard:
            created = self._entries.get(key)
            if created is None:
                return False
            if self._clock() - created >= self.ttl_seconds:
                self._entries.pop(key, None)
                return False
            self._entries.move_to_end(key)
            return True

    def mark(self, key: ActivationKey) -> None:
        if key is None:
            return
        with self._guard:
            self._entries[key] = self._clock()
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, key: ActivationKey) -> None:
        if key is None:
            return
        with self._guard:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._guard:
            self._entries.clear()
            self._session_locks.clear()

    def session_guard(self, fp: str):
        """同一会话串行（含激活与整次查询）；不同会话互不阻塞。

        锁生命周期与会话指纹绑定：账本条目淘汰不影响锁；锁不做过期淘汰
        （进程内不同 fp 数量有限），因此不存在“淘汰后换新锁同时进入”。
        """
        with self._guard:
            lock = self._session_locks.get(fp)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[fp] = lock
        return lock


LEDGER = ActivationLedger()
