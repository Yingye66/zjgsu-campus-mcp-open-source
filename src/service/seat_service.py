# -*- coding: utf-8 -*-
"""图书馆选座服务（我去图书馆白标部署，只读）。

职责：从本机会话读 seat_cookie → 调 seat 适配器 → 输出统一观测结构。
边界：无写操作（预约/占座/退座不提供）；Cookie 不回显不进日志；
     会话失效如实报 session_expired；明文入口默认关闭（见 seat.py 传输安全）。

最小观测契约（2026-10-18 审查修复）：
- 成功：freshness.source=live、last_success_at、data_age_seconds、stale=false；
  并把成功结果写入本机 state/seat_last.json（按账号域隔离）。
- 取数失败：若有同账号旧成功快照 → status=partial + stale=true + 明确 as_of
  与数据年龄，不冒充此刻空位；无旧快照 → unavailable（不伪造空结论）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import src.auth.session as S
from ..adapters.campus.seat import MANUAL_GUIDE, fetch_seat_summary

_TZ = timezone(timedelta(hours=8))
_STATE_DIRNAME = "state"
_SEAT_STATE = "seat_last.json"

_FLOOR_ORDER = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


class SeatService:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)

    def _cookie(self, acct: str | None) -> str | None:
        """只用调用方已固定的账号域读取 Cookie，避免二次读取活动账号造成串写。"""
        if not acct:
            return None
        return (S.load_session(self.data_dir, acct) or {}).get("seat_cookie")

    def _state_path(self) -> Path:
        return self.data_dir / _STATE_DIRNAME / _SEAT_STATE

    def _load_last(self, acct: str) -> dict | None:
        path = self._state_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("account") != acct:
            return None
        if not isinstance(data.get("payload"), dict) or not data.get("saved_at"):
            return None
        return data

    def _save_last(self, acct: str, payload: dict, now: str) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"account": acct, "saved_at": now,
                                       "payload": payload}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def _age_seconds(now_s: str, saved_s: str) -> int:
        try:
            delta = (datetime.fromisoformat(now_s) - datetime.fromisoformat(saved_s))
            return max(0, int(delta.total_seconds()))
        except (TypeError, ValueError):
            return 0

    def _freshness(self, *, live: bool, now: str, saved_at: str | None,
                   age: int) -> dict:
        block = {
            "source": "live" if live else "stale_snapshot",
            "data_age_seconds": age,
            "cache_hit": False,
            "stale": not live,
        }
        if saved_at:
            block["last_success_at"] = now if live else saved_at
        return block

    @staticmethod
    def _payload(data: dict) -> dict:
        return {
            "semantic": "badge=空位/总数（双样本核验：grid_1 空闲数==徽章空位数）；"
                        "余座为查询时刻状态，分钟级变动",
            "areas": data["areas"],
            "by_floor": {
                fl: {"free": v["free"], "total": v["total"],
                     "free_rate": (round(v["free"] / v["total"], 3)
                                   if v["total"] else None)}
                for fl, v in sorted(data["by_floor"].items(),
                                    key=lambda kv: _FLOOR_ORDER.get(kv[0][:1], 99))
            },
            "rank_by_free_rate": data["rank_by_rate"][:5],
        }

    def get_seat_summary(self) -> dict:
        now = datetime.now(_TZ).isoformat(timespec="seconds")
        acct = S.get_active(self.data_dir)
        cookie = self._cookie(acct)
        if not cookie:
            return {
                "status": "unavailable",
                "module_id": "library.seat",
                "query_time": now,
                "reason": "未导入选座会话 Cookie（手机抓包或真机 DevTools 收割后，"
                          "运行 login_cli seat-cookie / 收割脚本导入）",
                "manual_path": MANUAL_GUIDE,
                "gaps": ["预约/占座/退座等写操作不提供（红线）"],
            }
        res = fetch_seat_summary(cookie)
        if res["outcome"] == "ok":
            done_s = datetime.now(_TZ).isoformat(timespec="seconds")
            payload = self._payload(res["data"])
            self._save_last(acct or "", payload, done_s)
            return {
                "status": "ok",
                "module_id": "library.seat",
                "query_time": now,
                **payload,
                "session_note": ("会话 Cookie 模式：失效后需重新收割（手机端一次性操作）；"
                                 "source=live 仅代表本次成功读取，余座本身实时变动，"
                                 "不代表查询完成后的当前状态"),
                "freshness": self._freshness(live=True, now=done_s,
                                             saved_at=done_s, age=0),
                "issues": [],
                "gaps": ["预约/占座/退座等写操作不提供（红线）"],
            }

        # 取数失败：同账号旧成功快照 → 明确 partial/stale；否则 unavailable
        last = self._load_last(acct or "")
        if last is not None:
            saved_at = last["saved_at"]
            age = self._age_seconds(now, saved_at)
            return {
                "status": "partial",
                "module_id": "library.seat",
                "query_time": now,
                **last["payload"],
                "freshness": self._freshness(live=False, now=now,
                                             saved_at=saved_at, age=age),
                "issues": [{
                    "code": "upstream_error",
                    "retryable": True,
                    "message": "%s：%s" % (res["outcome"], res.get("diag", "")),
                }],
                "as_of": saved_at,
                "reason": "本次取数失败，以下为 %s 的旧快照，不代表此刻余座" % saved_at,
                "manual_path": MANUAL_GUIDE,
                "gaps": ["预约/占座/退座等写操作不提供（红线）"],
            }
        reason = "%s：%s" % (res["outcome"], res.get("diag", ""))
        return {
            "status": "unavailable",
            "module_id": "library.seat",
            "query_time": now,
            "reason": reason,
            "hint": ("session_expired 时请重新收割会话 Cookie（真机 USB + "
                     "test/harvest_seat_cookie.py，或手机抓包 + login_cli seat-cookie）；"
                     "明文 HTTP 默认关闭，见 reason"),
            "manual_path": MANUAL_GUIDE,
            "freshness": self._freshness(live=False, now=now,
                                         saved_at=None, age=0),
            "issues": [{"code": "upstream_error", "retryable": True,
                        "message": reason}],
            "gaps": ["预约/占座/退座等写操作不提供（红线）"],
        }
