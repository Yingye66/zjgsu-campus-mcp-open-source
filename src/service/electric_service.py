# -*- coding: utf-8 -*-
"""寝室电表服务（北电仪表「电精灵」，bd.bdfairy.cn，只读）。

职责：读本机 data/dorm_meter.json 定位（房间→表号）→ 调 electric 适配器 →
输出统一观测结构。表号缺失/失效时按需级联重解析并回写配置。

边界：无写操作（充值/缴费不提供，红线）；表号与房间信息仅存本机私有配置，
不回显在 unavailable 文案之外的结构化输出里（summary 原样带表号，本人使用）。

最小观测契约（2026-10-18 审查修复）：
- 成功：freshness.source=live、last_success_at、data_age_seconds、stale=false；
  成功结果按「表号+模式+月份」写入本机 state/electric_last.json。
- 取数失败：若有同一身份的旧成功快照 → status=partial + stale=true + as_of，
  不把旧余额冒充当前余额；无旧快照 → unavailable。
- 设备级配置须明确园区归属（area_root_id）；匿名可查不等于本人账号授权。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.campus import electric

_TZ = timezone(timedelta(hours=8))
_STATE_DIRNAME = "state"
_ELECTRIC_STATE = "electric_last.json"

_STATUS_ZH = {
    "合闸": "合闸（供电中）",
    "保电": "保电（保护模式，欠费不自动断闸）",
    "拉闸": "拉闸（已断电）",
}


class ElectricService:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "dorm_meter.json"

    # ---------- 配置 ----------

    def _load_config(self) -> dict | None:
        if not self.config_path.is_file():
            return None
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_config(self, cfg: dict) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _location_key(cfg: dict) -> str | None:
        """园区+楼栋+楼层+房间的稳定绑定标识；直接表号模式返回 None。"""
        parts = [str(cfg.get(k) or "").strip() for k in
                 ("area_root_id", "building", "floor", "room")]
        if not any(parts):
            return None
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _meter(self, cfg: dict | None) -> tuple[str | None, str | None]:
        """解析表号；配置缺表号时按需级联重解析并回写。"""
        if not cfg:
            return None, "未配置寝室定位（data/dorm_meter.json：building/floor/room/meter）"
        meter = str(cfg.get("meter") or "").strip()
        current_loc = self._location_key(cfg)
        bound_loc = str(cfg.get("location_key") or "").strip() or None
        if meter:
            if current_loc and bound_loc and current_loc != bound_loc:
                return None, ("配置定位与表号绑定不一致（房间/园区已变化）；"
                              "请删除 meter 后重新解析，或恢复原定位")
            if current_loc and not bound_loc:
                # 旧配置迁移前核对现存证据：状态里该表号的任一非空定位字段
                # （楼栋/楼层/房间/园区 id）与配置矛盾 → 拒绝盖章迁移
                for entry in self._load_all().values():
                    if not isinstance(entry, dict) or entry.get("meter") != meter:
                        continue
                    data = (entry.get("payload") or {}).get("data") or {}
                    old_room = data.get("room") or {}
                    for field in ("building", "floor", "room", "area_root_id"):
                        old_v = str(old_room.get(field) or "").strip()
                        new_v = str((cfg or {}).get(field) or "").strip()
                        if old_v and new_v and old_v != new_v:
                            return None, ("现有成功记录显示表号 %s 的 %s 为 %s，与配置 %s "
                                          "矛盾；请删除 meter 后重新解析（不盲目迁移绑定）"
                                          % (meter, field, old_v, new_v))
                # 无矛盾证据：以当前定位建立绑定，之后变化即可检测
                self._save_config({**cfg, "location_key": current_loc})
            return meter, None
        building, floor, room = (cfg.get("building"), cfg.get("floor"),
                                 cfg.get("room"))
        if not (building and floor and room):
            return None, "配置缺少表号且房间定位不完整（building/floor/room）"
        res = electric.resolve_room_meter(
            str(building), str(floor), str(room),
            area_label=str(cfg.get("area_label") or "钱江湾学生公寓"),
            area_root_id=(str(cfg.get("area_root_id") or "").strip() or None))
        if res["outcome"] != "ok":
            hint = ("；同名楼栋/楼层/房间跨园区命中，请在 data/dorm_meter.json "
                    "指定 area_root_id（如钱江湾 001000009）"
                    if res["outcome"] == "ambiguous" else "")
            return None, "表号解析失败（%s）：%s%s" % (
                res["outcome"], res["diag"], hint)
        cfg = {**cfg, "meter": res["meter"],
               "area_root_id": (res.get("root_id")
                                or cfg.get("area_root_id")),
               "updated_at": datetime.now(_TZ).isoformat(timespec="seconds")}
        cfg["location_key"] = self._location_key(cfg)
        self._save_config(cfg)
        return res["meter"], None

    # ---------- 旧成功快照 ----------

    def _state_path(self) -> Path:
        return self.data_dir / _STATE_DIRNAME / _ELECTRIC_STATE

    @staticmethod
    def _key(mode: str, month: str | None, meter: str) -> str:
        raw = "%s|%s|%s" % (mode, month or "", meter)
        return "em-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _load_all(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    def _load_last(self, key: str, meter: str) -> dict | None:
        entry = self._load_all().get(key)
        if not isinstance(entry, dict) or entry.get("meter") != meter:
            return None
        if not isinstance(entry.get("payload"), dict) or not entry.get("saved_at"):
            return None
        return entry

    def _save_last(self, key: str, meter: str, payload: dict, now: str) -> None:
        entries = self._load_all()
        entries[key] = {"meter": meter, "saved_at": now, "payload": payload}
        # 只保留最近 8 个身份（表号+模式+月份），避免无限增长
        if len(entries) > 8:
            ordered = sorted(entries.items(),
                             key=lambda kv: kv[1].get("saved_at", ""),
                             reverse=True)[:8]
            entries = dict(ordered)
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def _age_seconds(now_s: str, saved_s: str) -> int:
        try:
            return max(0, int((datetime.fromisoformat(now_s)
                               - datetime.fromisoformat(saved_s)).total_seconds()))
        except (TypeError, ValueError):
            return 0

    # ---------- 输出 ----------

    @staticmethod
    def _status_zh(raw: str | None) -> str | None:
        if not raw:
            return None
        key = raw.split("-")[0].strip()
        return _STATUS_ZH.get(key, raw)

    @staticmethod
    def _freshness(*, live: bool, now: str, saved_at: str | None,
                   age: int) -> dict:
        block = {"source": "live" if live else "stale_snapshot",
                 "data_age_seconds": age, "cache_hit": False,
                 "stale": not live}
        if saved_at:
            block["last_success_at"] = now if live else saved_at
        return block

    def _ok(self, payload: dict, now: str, done_s: str) -> dict:
        """query_time=请求开始；last_success_at=取数完成，二者分离。"""
        return {"status": "ok", "module_id": "dorm.utility",
                "query_time": now, **payload,
                "freshness": self._freshness(live=True, now=done_s,
                                             saved_at=done_s, age=0),
                "issues": []}

    def _stale(self, last: dict, now: str, reason: str) -> dict:
        saved_at = last["saved_at"]
        return {"status": "partial", "module_id": "dorm.utility",
                "query_time": now, **last["payload"],
                "freshness": self._freshness(
                    live=False, now=now, saved_at=saved_at,
                    age=self._age_seconds(now, saved_at)),
                "as_of": saved_at,
                "reason": "本次取数失败，以下为 %s 的旧快照，不代表当前余额/闸位"
                          % saved_at,
                "issues": [{"code": "upstream_error", "retryable": True,
                            "message": reason}],
                }

    def _fail(self, reason: str, now: str) -> dict:
        return {"status": "unavailable", "module_id": "dorm.utility",
                "query_time": now, "reason": reason,
                "freshness": self._freshness(live=False, now=now,
                                             saved_at=None, age=0),
                "issues": [{"code": "upstream_error", "retryable": True,
                            "message": reason}],
                "gaps": ["充值/缴费等写操作不提供（红线）"]}

    def get_dorm_electric(self, mode: str = "summary",
                          month: str | None = None) -> dict:
        now = datetime.now(_TZ).isoformat(timespec="seconds")
        mode = (mode or "summary").strip().lower()
        if mode not in ("summary", "bill", "detail"):
            return {
                "status": "unavailable",
                "module_id": "dorm.utility",
                "query_time": now,
                "reason": "mode 仅支持 summary（余额/状态）、bill（月账单）、"
                          "detail（日明细）",
                "gaps": ["充值/缴费等写操作不提供（红线）"],
            }
        cfg = self._load_config()
        meter, err = self._meter(cfg)
        if not meter:
            return {
                "status": "unavailable",
                "module_id": "dorm.utility",
                "query_time": now,
                "reason": err,
                "hint": "在 data/dorm_meter.json 填入寝室定位（building/floor/room"
                        " 或直接 meter；同名楼栋请补 area_root_id）；"
                        "表号可在微信「电精灵」页面查看",
                "gaps": ["充值/缴费等写操作不提供（红线）"],
            }
        month_s = (month or "").strip() or datetime.now(_TZ).strftime("%Y-%m")
        key = self._key(mode, month_s if mode == "detail" else None, meter)

        if mode == "summary":
            res = electric.fetch_meter_summary(meter)
            if res["outcome"] != "ok":
                last = self._load_last(key, meter)
                reason = "%s：%s" % (res["outcome"], res["diag"])
                if last:
                    return self._stale(last, now, reason)
                return self._fail(
                    reason + "（empty=表号无效；error=服务端偶发无数据已重试）",
                    now)
            d = res["data"]
            payload = {
                "semantic": "余额为本次来源返回值；reading_time=来源抄表时间"
                            "（刷新规律尚未独立验证）；匿名公开链路，非本人账号授权；"
                            "source=live 仅代表本次成功读取，不代表源数据此刻状态或设备归属",
                "data": {
                    "meter": d["meter"],
                    "room": {
                        "area": (cfg or {}).get("area_label"),
                        "building": (cfg or {}).get("building"),
                        "floor": (cfg or {}).get("floor"),
                        "room": (cfg or {}).get("room"),
                        "area_root_id": (cfg or {}).get("area_root_id"),
                    },
                    "prepaid_yuan": d["prepaid_yuan"],
                    "subsidy_yuan": d["subsidy_yuan"],
                    "total_yuan": d["total_yuan"],
                    "total_kwh": d["total_kwh"],
                    "meter_status": d["meter_status"],
                    "meter_status_zh": self._status_zh(d["meter_status"]),
                    "reading_time": d["reading_time"],
                },
                "gaps": ["充值/缴费等写操作不提供（红线）；水表未单独接入"
                         "（water_main 对同表号返回同一电表数据）"],
            }
            done_s = datetime.now(_TZ).isoformat(timespec="seconds")
            self._save_last(key, meter, payload, done_s)
            return self._ok(payload, now, done_s)

        if mode == "bill":
            res = electric.fetch_meter_bill(meter)
            if res["outcome"] != "ok":
                last = self._load_last(key, meter)
                reason = "%s：%s" % (res["outcome"], res["diag"])
                if last:
                    return self._stale(last, now, reason)
                return self._fail(reason, now)
            payload = {
                "semantic": "qcdl=期初示数 qmdl=期末示数 ydl=用电量(度) "
                            "ydje=用电金额(元)；month 为来源页默认最近月",
                "data": res["data"],
                "gaps": ["历史月份账单来源页未提供稳定入参，仅返回最近月"],
            }
            done_s = datetime.now(_TZ).isoformat(timespec="seconds")
            self._save_last(key, meter, payload, done_s)
            return self._ok(payload, now, done_s)

        # detail
        res = electric.fetch_meter_detail(meter, month_s)
        if res["outcome"] != "ok":
            last = self._load_last(key, meter)
            reason = "%s：%s" % (res["outcome"], res["diag"])
            if last:
                return self._stale(last, now, reason)
            return self._fail(reason, now)
        d = res["data"]
        payload = {
            "semantic": "日用电明细（date=yyyyMMdd，kwh=当日用电量，yuan=当日金额）；"
                        "仅含有用量记录的日期",
            "data": d,
            "gaps": ["充值/缴费等写操作不提供（红线）"],
        }
        done_s = datetime.now(_TZ).isoformat(timespec="seconds")
        self._save_last(key, meter, payload, done_s)
        return self._ok(payload, now, done_s)
