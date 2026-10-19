# -*- coding: utf-8 -*-
"""缓存库维护：一致性备份 / WAL checkpoint / 状态查看。

- backup 使用 sqlite3 backup API：对 WAL 库得到**一致快照**（含未 checkpoint 的
  WAL 内容），目标库是独立普通文件，可直接归档/外传前再人工检查隐私。
- checkpoint(TRUNCATE)：把 WAL 收缩为 0；只应在无活动查询时执行（本工具单机使用）。
- 红线不变：备份文件与库本身同属私有数据，放在 data/ 之外时需自行保证权限。

用法：
    python -m src.storage.maintenance status
    python -m src.storage.maintenance checkpoint
    python -m src.storage.maintenance backup --dest D:/backup/cache-20261018.sqlite3
    （可用 --db 指定库路径；默认 default_data_dir()/cache.sqlite3）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..paths import default_data_dir

_TZ = timezone(timedelta(hours=8))


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def status(db_path: Path) -> dict:
    con = _connect(db_path)
    try:
        journal = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        page_count = int(con.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(con.execute("PRAGMA page_size").fetchone()[0])
        snaps = int(con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
        health = int(con.execute("SELECT COUNT(*) FROM source_health").fetchone()[0])
    finally:
        con.close()
    wal = Path(str(db_path) + "-wal")
    return {"db": str(db_path), "journal_mode": journal,
            "size_bytes": db_path.stat().st_size if db_path.is_file() else 0,
            "wal_bytes": wal.stat().st_size if wal.is_file() else 0,
            "page_count": page_count, "page_size": page_size,
            "snapshots": snaps, "source_health": health}


def checkpoint(db_path: Path) -> dict:
    con = _connect(db_path)
    try:
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    wal = Path(str(db_path) + "-wal")
    return {"db": str(db_path), "busy": bool(row[0]),
            "log_pages": int(row[1]), "checkpointed_pages": int(row[2]),
            "wal_bytes_after": wal.stat().st_size if wal.is_file() else 0}


def backup(db_path: Path, dest: Path, overwrite: bool = False) -> dict:
    src = Path(db_path)
    dest = Path(dest)
    if not src.is_file():
        raise FileNotFoundError("源库不存在：%s" % src)
    try:
        same = dest.exists() and src.samefile(dest)
    except OSError:
        same = False
    if same or src.resolve() == dest.resolve():
        raise ValueError("备份目标与源库相同（拒绝自备份）：%s" % dest)
    if dest.exists() and not overwrite:
        raise FileExistsError("目标已存在，未覆盖：%s（确认覆盖请加 --force）" % dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src_con = _connect(src)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src_con.backup(dst)      # 一致快照（含 WAL 内容）
            dst.commit()
        finally:
            dst.close()
    finally:
        src_con.close()
    return {"source": str(src), "dest": str(dest),
            "size_bytes": dest.stat().st_size,
            "created_at": datetime.now(_TZ).isoformat(timespec="seconds")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="缓存库维护（backup/checkpoint/status）")
    parser.add_argument("action", choices=("status", "checkpoint", "backup"))
    parser.add_argument("--db", default=None, help="库路径（默认 data/cache.sqlite3）")
    parser.add_argument("--dest", default=None, help="backup 目标路径")
    parser.add_argument("--force", action="store_true",
                        help="允许覆盖已存在的备份目标（默认拒绝）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path(default_data_dir()) / "cache.sqlite3"
    if not db_path.is_file():
        print("库不存在：%s" % db_path)
        return 1
    try:
        if args.action == "status":
            print(__import__("json").dumps(status(db_path), ensure_ascii=False, indent=2))
        elif args.action == "checkpoint":
            print(__import__("json").dumps(checkpoint(db_path), ensure_ascii=False, indent=2))
        else:
            if not args.dest:
                stamp = datetime.now(_TZ).strftime("%Y%m%d-%H%M%S")
                args.dest = str(db_path.parent / ("backup-%s.sqlite3" % stamp))
            print(__import__("json").dumps(backup(db_path, Path(args.dest),
                                                  overwrite=args.force),
                                           ensure_ascii=False, indent=2))
    except (ValueError, FileExistsError) as e:
        print("拒绝：%s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
