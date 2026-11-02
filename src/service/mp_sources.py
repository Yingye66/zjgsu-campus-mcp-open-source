# -*- coding: utf-8 -*-
"""公众号来源登记表：区分「已确认学校来源」与「名称候选」。

事实边界（写入本模块是为了防止后续维护把候选当官方）：
- 名称命中（含学校关键词）只表示**疑似**学校相关，不构成官方账号认定；
- **权威身份只按稳定 ID 绑定**：``confirmed``/``excluded`` 必须命中登记表的 ID；
  只写名称的条目不能授予确认或排除，最多作为候选提示；
- 不同 ID 同名不算同一账号；同名不得把 A 的确认/排除套到 B 上；
- ``pending`` 且无学校关联证据的普通订阅**不进入**学校范围；
- 登记文件缺失或损坏时如实返回错误码，调用方必须拒绝或明确降级，
  不得静默丢掉排除规则、也不得扩大到全部订阅。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..paths import default_data_dir

# 名称口径：只用于发现候选，不用于认定官方归属。
SCHOOL_TOKENS = ("浙江工商大学", "浙商大", "浙江工商", "ZJGSU", "zjgsu")

# 分类规则按具体度排序，命中即止；纯名称规则，可被登记表 category 覆盖。
CATEGORY_RULES = (
    ("教务", ("教务处", "教务")),
    ("图书馆", ("图书馆",)),
    ("就业", ("就业", "招聘", "职业发展", "创业")),
    ("招生", ("招生",)),
    ("研究生", ("研究生", "硕士", "博士")),
    ("校友", ("校友", "教育基金会")),
    ("团学", ("学生会", "团委", "志愿者", "社团")),
    ("学院", ("学院", "系", "研究院", "研究中心")),
    ("校级", ("浙江工商大学", "浙商大")),
)
DEFAULT_CATEGORY = "其他"

def contains_school_token(name) -> bool:
    """名称是否含学校关键词（候选发现口径，非官方认定）。"""
    if not isinstance(name, str) or not name:
        return False
    return any(token in name for token in SCHOOL_TOKENS)


def guess_category(name) -> str:
    if not isinstance(name, str) or not name:
        return DEFAULT_CATEGORY
    for category, tokens in CATEGORY_RULES:
        if any(token in name for token in tokens):
            return category
    return DEFAULT_CATEGORY


def registry_path(data_dir=None) -> Path:
    """登记表位置：环境变量 > 调用方目录 > 项目 data/。"""
    value = os.environ.get("ZJGSU_MP_SOURCES")
    if value:
        return Path(value)
    if data_dir is not None:
        return Path(data_dir) / "mp_sources.json"
    return default_data_dir() / "mp_sources.json"


def _normalize_entry(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    if "id" in raw and not isinstance(raw["id"], str):
        return None
    sid = raw.get("id")
    name = raw.get("name")
    sid = sid.strip() if isinstance(sid, str) else ""
    name = name.strip() if isinstance(name, str) else ""
    if not sid and not name:
        return None
    if len(sid) > 255 or len(name) > 255:
        return None
    status = raw.get("status", "pending")
    if not isinstance(status, str) or status.strip().lower() not in ("confirmed", "pending", "excluded"):
        return None
    if status.strip().lower() == "excluded" and not sid:
        return None
    category = raw.get("category")
    category = category.strip()[:40] if isinstance(category, str) and category.strip() else None
    aliases = raw.get("aliases")
    if aliases is None:
        aliases = []
    # A single textual alias is a convenient equivalent of a one-element list.
    # Identity and exclusion remain bound to the validated ID, never to aliases.
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        return None
    aliases = [a.strip() for a in aliases
               if isinstance(a, str) and a.strip() and len(a.strip()) <= 255][:20]
    note = raw.get("note")
    note = note.strip()[:200] if isinstance(note, str) and note.strip() else ""
    evidence = raw.get("school_evidence")
    evidence = evidence.strip()[:200] if isinstance(evidence, str) and evidence.strip() else ""
    return {"id": sid, "name": name, "status": status.strip().lower(),
            "category": category, "aliases": aliases, "note": note,
            "school_evidence": evidence}


def load_registry(path=None) -> tuple[list[dict], dict]:
    """读取登记表；返回 (entries, meta)。meta 不含本机路径，只含状态码与计数。"""
    target = Path(path) if path is not None else registry_path()
    try:
        target.stat()
    except FileNotFoundError:
        return [], {"configured": False, "error": None, "invalid_entries": 0}
    except OSError:
        return [], {"configured": True, "error": "registry_unreadable", "invalid_entries": 0}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {"configured": True, "error": "registry_unreadable",
                    "invalid_entries": 0}
    raw_entries = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        return [], {"configured": True, "error": "registry_invalid",
                    "invalid_entries": 0}
    entries, invalid = [], 0
    for raw in raw_entries:
        entry = _normalize_entry(raw)
        if entry is None:
            invalid += 1
        else:
            entries.append(entry)
    meta = {"configured": True, "error": None, "invalid_entries": invalid}
    return entries, meta


def build_index(entries) -> tuple[dict, dict, int]:
    """按 ID 建权威索引，另建「仅名称」提示索引。

    - 同一 ID 重复且状态/分类一致：取首条（等价于去重）；
    - 同一 ID 重复但状态或分类冲突：标记为 ``conflict``，不授予确认/排除；
    - 仅名称条目（无 ID）：只作候选提示，不能确认也不能排除；同名冲突则丢弃。
    """
    by_id: dict[str, dict] = {}
    name_hints: dict[str, dict] = {}
    conflicts = 0
    for entry in entries:
        if entry["id"]:
            previous = by_id.get(entry["id"])
            if previous is None:
                by_id[entry["id"]] = dict(entry)
            elif previous["status"] == "conflict":
                continue
            elif (previous["status"], previous["category"]) != (entry["status"], entry["category"]):
                by_id[entry["id"]] = {"id": entry["id"], "name": previous["name"],
                                      "status": "conflict", "category": None,
                                      "aliases": [], "note": "登记表内同 ID 状态冲突",
                                      "school_evidence": ""}
                conflicts += 1
        else:
            previous = name_hints.get(entry["name"])
            if previous is None:
                name_hints[entry["name"]] = dict(entry)
            elif previous["status"] != entry["status"]:
                name_hints.pop(entry["name"], None)
                conflicts += 1
    return by_id, name_hints, conflicts


def resolve_sources(feeds, entries, scope: str) -> dict:
    """按 scope 计算来源集合。feeds 为 [(id, mp_name)]，entries 为登记表。

    ``scope="school"``：ID 已确认来源 + 名称候选（含学校关键词或有学校关联证据的待核实条目），
    ID 排除项剔除；无学校关联证据的普通 pending 订阅不进入。
    ``scope="school_confirmed"``：仅 ID 已确认来源。

    返回 allowed / confirmed / pending / excluded / unclassified / conflicts。
    """
    by_id, name_hints, conflicts = build_index(entries)
    allowed: list[dict] = []
    confirmed = pending = excluded = unclassified = 0
    for feed_id, name in feeds:
        feed_id = feed_id or ""
        name = name or ""
        entry = by_id.get(feed_id)
        status = entry["status"] if entry else None
        if status == "conflict":
            # 冲突条目无权威身份：不确认、不排除，也不靠名称自证。
            entry, status = None, None
        if status == "excluded":
            excluded += 1
            continue
        if status == "confirmed":
            allowed.append({"mp_id": feed_id, "mp_name": name,
                            "category": entry["category"] or guess_category(name),
                            "status": "confirmed",
                            "basis": "registry_id"})
            confirmed += 1
            continue
        if scope == "school_confirmed":
            if status != "confirmed":
                unclassified += 1
            continue
        if status == "pending" and entry["school_evidence"]:
            allowed.append({"mp_id": feed_id, "mp_name": name,
                            "category": entry["category"] or guess_category(name),
                            "status": "pending", "basis": "registry_evidence"})
            pending += 1
            continue
        if contains_school_token(name):
            allowed.append({"mp_id": feed_id, "mp_name": name,
                            "category": (entry["category"] if entry else None)
                            or guess_category(name),
                            "status": "pending", "basis": "name_token"})
            pending += 1
            continue
        hint = name_hints.get(name)
        if hint is not None and hint["school_evidence"]:
            # 仅名称条目只能给候选，不能给确认或排除。
            allowed.append({"mp_id": feed_id, "mp_name": name,
                            "category": hint["category"] or guess_category(name),
                            "status": "pending", "basis": "name_only_evidence"})
            pending += 1
            continue
        unclassified += 1
    return {"allowed": allowed, "confirmed": confirmed, "pending": pending,
            "excluded": excluded, "unclassified": unclassified,
            "conflicts": conflicts}
