# -*- coding: utf-8 -*-
"""本机缓存（SQLite）：按「账号 + 模块 + 来源 + 规范化查询范围」隔离的快照与来源健康。

隔离与保留语义（对应交接文档第 2.6 节最小验收 ①②③）：
- 快照主键含 account_scope_id——两个账号、两个日期范围互不读取；
- 成功快照（含已知成功码的合法空数组）upsert 替换；失败路径不触碰快照与
  last_success_at，只更新 last_attempt_at / last_issue；
- 未知业务码在进入本层之前已被拒绝（见 adapters/campus/classify.py），本层不做放宽。

表结构：
- snapshots: 每 (account,module,source,scope) 一行，成功快照（含空快照证据位）；
- source_health: 每 (account,module,source) 一行，最近尝试/成功/问题。

时间以带 +08:00 偏移的 ISO 8601 字符串存储（同偏移下字符串顺序 = 时间顺序）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  account_scope_id TEXT NOT NULL,
  module_id        TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  query_scope      TEXT NOT NULL,
  snapshot_id      TEXT NOT NULL,
  fetched_at       TEXT NOT NULL,
  complete         INTEGER NOT NULL DEFAULT 0,
  consistency      TEXT NOT NULL DEFAULT 'best_effort',
  missing_ranges   TEXT NOT NULL DEFAULT '[]',
  empty_evidence   INTEGER NOT NULL DEFAULT 0,
  items_json       TEXT NOT NULL,
  PRIMARY KEY (account_scope_id, module_id, source_id, query_scope)
);
CREATE TABLE IF NOT EXISTS source_health (
  account_scope_id TEXT NOT NULL,
  module_id        TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  last_attempt_at  TEXT,
  last_success_at  TEXT,
  last_issue_json  TEXT,
  last_query_scope TEXT,
  PRIMARY KEY (account_scope_id, module_id, source_id)
);
"""


class SnapshotStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = self._conn()
        try:
            # WAL：避免每次提交都创建/删除 journal；synchronous=FULL 仍保证提交落盘，
            # 不采用 NORMAL（掉电可能回滚最近事务）。DB 只是可重取的缓存。
            # ZJGSU_STORE_JOURNAL=DELETE 可强制回退旧行为（排障/对比用）。
            mode = (os.environ.get("ZJGSU_STORE_JOURNAL", "WAL") or "WAL").upper()
            if mode not in ("WAL", "DELETE"):
                mode = "WAL"
            con.execute("PRAGMA journal_mode=%s" % mode)
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=FULL")
        # 显式声明默认行为（1000 页 ≈ 4MB @4KB）：WAL 达到阈值自动 checkpoint，
        # 防止长期运行下 -wal 无限增长；维护可用 src/storage/maintenance.py。
        con.execute("PRAGMA wal_autocheckpoint=1000")
        return con

    def journal_mode(self) -> str:
        con = self._conn()
        try:
            return str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            con.close()

    # ---------- 快照 ----------
    def get_snapshot(self, account: str, module: str, source: str, scope: str) -> dict | None:
        con = self._conn()
        try:
            row = con.execute(
                "SELECT * FROM snapshots WHERE account_scope_id=? AND module_id=? AND source_id=? AND query_scope=?",
                (account, module, source, scope)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return {
            "snapshot_id": row["snapshot_id"],
            "fetched_at": row["fetched_at"],
            "complete": bool(row["complete"]),
            "consistency": row["consistency"],
            "missing_ranges": json.loads(row["missing_ranges"]),
            "empty_evidence": bool(row["empty_evidence"]),
            "items": json.loads(row["items_json"]),
        }

    @staticmethod
    def _upsert_snapshot_con(con, account: str, module: str, source: str, scope: str,
                             items: list, fetched_at: str, complete: bool,
                             consistency: str, missing_ranges: list | None,
                             empty_evidence: bool) -> dict:
        snap_id = "snap-" + uuid.uuid4().hex[:12]
        con.execute(
            "INSERT INTO snapshots (account_scope_id, module_id, source_id, query_scope,"
            " snapshot_id, fetched_at, complete, consistency, missing_ranges, empty_evidence, items_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(account_scope_id, module_id, source_id, query_scope) DO UPDATE SET"
            " snapshot_id=excluded.snapshot_id, fetched_at=excluded.fetched_at,"
            " complete=excluded.complete, consistency=excluded.consistency,"
            " missing_ranges=excluded.missing_ranges, empty_evidence=excluded.empty_evidence,"
            " items_json=excluded.items_json",
            (account, module, source, scope, snap_id, fetched_at,
             1 if complete else 0, consistency,
             json.dumps(missing_ranges or [], ensure_ascii=False),
             1 if empty_evidence else 0,
             json.dumps(items or [], ensure_ascii=False)))
        return {"snapshot_id": snap_id, "fetched_at": fetched_at, "complete": complete,
                "consistency": consistency, "missing_ranges": missing_ranges or [],
                "empty_evidence": empty_evidence, "items": items or []}

    def upsert_snapshot(self, account: str, module: str, source: str, scope: str,
                        items: list, fetched_at: str, complete: bool, consistency: str,
                        missing_ranges: list | None = None, empty_evidence: bool = False) -> dict:
        con = self._conn()
        try:
            snap = self._upsert_snapshot_con(
                con, account, module, source, scope, items, fetched_at, complete,
                consistency, missing_ranges, empty_evidence)
            con.commit()
        finally:
            con.close()
        return snap

    def upsert_snapshot_and_health(self, account: str, module: str, source: str,
                                   scope: str, items: list, fetched_at: str,
                                   complete: bool, consistency: str,
                                   missing_ranges: list | None = None,
                                   empty_evidence: bool = False, *,
                                   success: bool, at: str | None = None,
                                   issue: dict | None = None) -> dict:
        """快照与健康记录写入**同一事务**：不会出现「新快照 + 旧 last_success」的中间态。

        success=True → 推进 last_success_at 并清空 last_issue；
        success=False → 只登记 last_issue（部分观测），不推进 last_success。
        """
        con = self._conn()
        try:
            snap = self._upsert_snapshot_con(
                con, account, module, source, scope, items, fetched_at, complete,
                consistency, missing_ranges, empty_evidence)
            self._ensure_health_row(con, account, module, source)
            if success:
                con.execute(
                    "UPDATE source_health SET last_success_at=?, last_issue_json=NULL,"
                    " last_query_scope=?"
                    " WHERE account_scope_id=? AND module_id=? AND source_id=?",
                    (at or fetched_at, scope, account, module, source))
            else:
                con.execute(
                    "UPDATE source_health SET last_issue_json=?, last_query_scope=?"
                    " WHERE account_scope_id=? AND module_id=? AND source_id=?",
                    (json.dumps(issue or {}, ensure_ascii=False), scope,
                     account, module, source))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return snap

    # ---------- 来源健康 ----------
    def _ensure_health_row(self, con, account: str, module: str, source: str) -> None:
        con.execute(
            "INSERT OR IGNORE INTO source_health (account_scope_id, module_id, source_id)"
            " VALUES (?,?,?)", (account, module, source))

    def record_attempt(self, account: str, module: str, source: str, at: str, scope: str) -> None:
        con = self._conn()
        try:
            self._ensure_health_row(con, account, module, source)
            con.execute(
                "UPDATE source_health SET last_attempt_at=?, last_query_scope=?"
                " WHERE account_scope_id=? AND module_id=? AND source_id=?",
                (at, scope, account, module, source))
            con.commit()
        finally:
            con.close()

    def record_success(self, account: str, module: str, source: str, at: str, scope: str) -> None:
        con = self._conn()
        try:
            self._ensure_health_row(con, account, module, source)
            con.execute(
                "UPDATE source_health SET last_success_at=?, last_issue_json=NULL, last_query_scope=?"
                " WHERE account_scope_id=? AND module_id=? AND source_id=?",
                (at, scope, account, module, source))
            con.commit()
        finally:
            con.close()

    def record_issue(self, account: str, module: str, source: str, at: str, scope: str,
                     issue: dict) -> None:
        con = self._conn()
        try:
            self._ensure_health_row(con, account, module, source)
            con.execute(
                "UPDATE source_health SET last_issue_json=?, last_query_scope=?"
                " WHERE account_scope_id=? AND module_id=? AND source_id=?",
                (json.dumps(issue, ensure_ascii=False), scope, account, module, source))
            con.commit()
        finally:
            con.close()

    def get_health_rows(self, account: str, module: str | None = None) -> list[dict]:
        con = self._conn()
        try:
            if module:
                rows = con.execute(
                    "SELECT * FROM source_health WHERE account_scope_id=? AND module_id=? ORDER BY source_id",
                    (account, module)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM source_health WHERE account_scope_id=? ORDER BY module_id, source_id",
                    (account,)).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            out.append({
                "module_id": r["module_id"],
                "source_id": r["source_id"],
                "last_attempt_at": r["last_attempt_at"],
                "last_success_at": r["last_success_at"],
                "last_issue": json.loads(r["last_issue_json"]) if r["last_issue_json"] else None,
                "last_query_scope": r["last_query_scope"],
            })
        return out
