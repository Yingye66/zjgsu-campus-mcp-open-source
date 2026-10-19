# -*- coding: utf-8 -*-
"""Experimental dorm-electric adapter (read-only); see docs/release-status.md.

Anonymous campus-wide endpoint; reachability does not establish device
ownership. Area/building matching refuses ambiguity instead of guessing.
No recharge/payment operations are provided, and no other rooms are queried.
Real device samples and balances are excluded from published sources.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime

import requests

BASE = "http://bd.bdfairy.cn"
AREA_ID = "74"
AREA_NAME = "浙江工商大学"

_UA = ("Mozilla/5.0 (Linux; Android 14; SM-G9910) AppleWebKit/537.36 "
       "Chrome/116.0.0.0 Mobile Safari/537.36 XWEB/1160065 "
       "MicroMessenger/8.0.42.2420")

_REF_RE = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*ref\(\s*'([^']*)'\s*\)")
_REF_RE2 = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*ref\(\s*\"([^\"]*)\"\s*\)")
_BILL_RE = re.compile(r"billData\s*=\s*ref\(\s*(\{[\s\S]*?\})\s*\)")
_BILL_KV_RE = re.compile(r"(\w+)\s*:\s*[\"']([^\"']*)[\"']")
_MONTH_RE = re.compile(r"month\s*=\s*ref\(\s*[\"']([^\"']+)[\"']\s*\)")
_DETAIL_RE = re.compile(
    r"comments\s*=\s*ref\(\s*JSON\.parse\(\s*'((?:\\'|[^'])*)'\s*\)\s*\)")
_COMADDR_RE = re.compile(r"['\"]comAddress['\"]\s*:\s*['\"](\d{6,})['\"]")
_COMADDR_RE2 = re.compile(r"comAddress['\"\s:=\)]+['\"]?(\d{8,})")
_MONTH_ARG_RE = re.compile(r"\A([0-9]{4})-(0[1-9]|1[0-2])\Z")
_ROOT_IDS = ["001000001", "001000002", "001000003", "001000004",
             "001000005", "001000006", "001000007", "001000008",
             "001000009", "001000010", "001000011", "001000012"]


def valid_month(month: str) -> bool:
    """month 必须是 YYYY-MM，年份限定 2000..次年（来源只提供近期账单）。"""
    m = _MONTH_ARG_RE.match(str(month or "").strip())
    if not m:
        return False
    year = int(m.group(1))
    return 2000 <= year <= datetime.now().year + 1


def _json_or_none(resp) -> dict | None:
    """受控解析上游 JSON；非 JSON 返回 None（不向上抛异常）。"""
    try:
        data = json.loads(_decode(resp))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _unique_by_name(items, name: str, key: str = "BuildingName") -> list:
    """按名称精确匹配（去空白），返回全部命中（用于歧义判定）。"""
    target = str(name or "").strip()
    if not target:
        return []
    return [x for x in (items or [])
            if isinstance(x, dict)
            and str(x.get(key) or "").strip() == target
            and x.get("BuildingID")]

_BALANCE_KEYS = ("yffye", "bzdye", "zye", "zydl", "dbzt", "cbsj",
                 "roomaddr", "chargeType")


def _decode(resp: requests.Response) -> str:
    raw = resp.content or b""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _hj(referer: str = "/login") -> dict:
    return {"Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE, "Referer": BASE + referer}


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "X-Requested-With": "com.tencent.mm",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def _bootstrap(timeout: float = 20.0) -> requests.Session:
    """匿名入口 + 选校（商大 areaId=74）。"""
    s = _new_session()
    s.get(f"{BASE}/?id={AREA_ID}", allow_redirects=False, timeout=timeout)
    s.post(f"{BASE}/selectarea", json={"areaId": AREA_ID, "areaName": AREA_NAME},
           headers=_hj("/selectArea"), timeout=timeout)
    return s


# ---------------- 匿名会话复用（只复用传输，不缓存查询结果） ----------------

_SESSION_TTL_SECONDS = 300.0
_SESSION_MAX = 4
_session_cache: "OrderedDict[str, tuple]" = OrderedDict()
_session_lock = threading.Lock()
_http_lock = threading.Lock()  # 电表请求低频；串行化以保护 Session 内部状态


def _close_quietly(s) -> None:
    try:
        s.close()
    except Exception:
        pass


def _drop_session(meter: str) -> None:
    with _session_lock:
        entry = _session_cache.pop(str(meter), None)
    if entry:
        _close_quietly(entry[0])


def clear_sessions() -> None:
    """关闭并清空进程内匿名会话（测试/退出用）。"""
    with _session_lock:
        entries = list(_session_cache.values())
        _session_cache.clear()
    for s, _ in entries:
        _close_quietly(s)


def _meter_session(meter: str, timeout: float = 20.0):
    """返回已完成「选校 + 选表」的匿名会话；失败返回 (None, 错误字典)。

    只复用连接与 session cookie，不缓存余额/账单/明细——每次查询仍取最新页面。
    表号无效（服务端拒绝）不进入缓存。
    """
    meter = str(meter).strip()
    now = time.monotonic()
    with _session_lock:
        entry = _session_cache.get(meter)
        if entry and now - entry[1] < _SESSION_TTL_SECONDS:
            _session_cache.move_to_end(meter)
            return entry[0], None
        if entry:
            _session_cache.pop(meter, None)
    try:
        s = _bootstrap(timeout)
        r = s.post(f"{BASE}/selectmeter",
                   json={"comAddress": meter, "hidType": "电表", "room": ""},
                   headers=_hj(), timeout=timeout)
        sm = _json_or_none(r)
        if sm is None:
            _close_quietly(s)
            return None, {"outcome": "parse_error",
                          "diag": "selectmeter 非 JSON 响应（上游结构可能变化）"}
        msg = str(sm.get("msg") or sm.get("message") or sm.get("error") or "")
        if msg and not sm.get("url"):
            _close_quietly(s)
            return None, {"outcome": "empty", "diag": "表号被拒绝: %s" % msg[:120]}
    except requests.RequestException as e:
        return None, {"outcome": "network_error",
                      "diag": "%s: %s" % (type(e).__name__, e)}
    with _session_lock:
        existing = _session_cache.get(meter)
        if existing and time.monotonic() - existing[1] < _SESSION_TTL_SECONDS:
            _close_quietly(s)
            _session_cache.move_to_end(meter)
            return existing[0], None
        _session_cache[meter] = (s, time.monotonic())
        _session_cache.move_to_end(meter)
        while len(_session_cache) > _SESSION_MAX:
            _, (old_s, _) = _session_cache.popitem(last=False)
            _close_quietly(old_s)
    return s, None


def _clean(v):
    if v is None:
        return None
    t = str(v).strip()
    if t.lower() in ("", "undefined", "null", "none"):
        return None
    return t


def _refs_of(html: str) -> dict:
    refs = dict(_REF_RE.findall(html))
    refs.update(dict(_REF_RE2.findall(html)))
    return refs


def _bill_object(html: str):
    m = _BILL_RE.search(html)
    if not m:
        return None
    out = {}
    for k, v in _BILL_KV_RE.findall(m.group(1)):
        out[k] = v
    return out or None


def _detail_rows(html: str):
    m = _DETAIL_RE.search(html)
    if not m:
        return None
    raw = (m.group(1).replace("\\'", "'").replace("\\n", "\n")
           .replace("\\\\", "\\"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def fetch_meter_summary(meter: str, timeout: float = 20.0,
                        max_attempts: int = 3) -> dict:
    """表号直查余额/状态（meter_main）。只读。

    返回 {'outcome': ok|empty|error|network_error, 'diag', 'data'}；
    data: {meter, prepaid_yuan, subsidy_yuan, total_yuan, total_kwh,
           meter_status, reading_time, room, charge_type}
    """
    meter = str(meter).strip()
    last_diag = ""
    last_outcome = "error"
    for _ in range(max(1, max_attempts)):
        with _http_lock:
            s, err = _meter_session(meter, timeout)
            if err is not None:
                last_diag = "%s：%s" % (err["outcome"], err["diag"])
                last_outcome = err["outcome"]
                if err["outcome"] == "empty":
                    return {"outcome": "empty", "diag": err["diag"], "data": None}
                continue
            try:
                r = s.get(BASE + "/meter_main",
                          headers={"Referer": BASE + "/selectmeter",
                                   "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                          allow_redirects=False, timeout=timeout)
            except requests.RequestException as e:
                _drop_session(meter)
                last_diag = "network: %s: %s" % (type(e).__name__, e)
                continue
        html = _decode(r)
        looks_error = (r.status_code != 200 or "errorApp" in html
                       or ("操作失败" in html and "yffye" not in html))
        refs = _refs_of(html)
        if looks_error or not (refs.get("yffye") or refs.get("zydl")):
            last_diag = ("meter_main 无数据 status=%s len=%d"
                         % (r.status_code, len(html)))
            _drop_session(meter)   # 服务端偶发空引用：换会话重试
            continue
        data = {"meter": meter}
        for k in _BALANCE_KEYS:
            data[k] = _clean(refs.get(k))
        return {"outcome": "ok", "diag": "", "data": {
            "meter": meter,
            "prepaid_yuan": data["yffye"],
            "subsidy_yuan": data["bzdye"],
            "total_yuan": data["zye"],
            "total_kwh": data["zydl"],
            "meter_status": data["dbzt"],
            "reading_time": data["cbsj"],
            "room": data["roomaddr"],
            "charge_type": data["chargeType"],
        }}
    return {"outcome": "parse_error" if last_outcome == "parse_error" else "error",
            "diag": last_diag or "多次重试仍失败", "data": None}


def fetch_meter_bill(meter: str, timeout: float = 20.0) -> dict:
    """表号月账单（electricbill，最近月份）。只读。

    返回 {'outcome', 'diag', 'data': {month, qcdl, qmdl, ydl, ydje} | None}
    """
    meter = str(meter).strip()
    last_diag = ""
    bill = None
    mm = None
    # 失效会话（非 200/errorApp）→ 淘汰池中会话并有限重建一次；不缓存错误结果
    for _attempt in range(2):
        with _http_lock:
            s, err = _meter_session(meter, timeout)
            if err is not None:
                return {**err, "data": None}
            try:
                r = s.get(f"{BASE}/electricbill",
                          headers={"Referer": BASE + "/meter_main"},
                          allow_redirects=False, timeout=timeout)
            except requests.RequestException as e:
                _drop_session(meter)
                return {"outcome": "network_error",
                        "diag": "%s: %s" % (type(e).__name__, e), "data": None}
        html = _decode(r)
        if r.status_code != 200 or "errorApp" in html:
            _drop_session(meter)
            last_diag = "月账单页不可用 status=%s" % r.status_code
            continue
        if "billData" not in html:
            return {"outcome": "error", "diag": "billData 解析失败（可能改版）",
                    "data": None}
        bill = _bill_object(html)
        mm = _MONTH_RE.search(html)
        break
    else:
        return {"outcome": "error", "diag": last_diag or "月账单页不可用",
                "data": None}
    if not bill:
        return {"outcome": "error", "diag": "billData 解析失败（可能改版）",
                "data": None}
    return {"outcome": "ok", "diag": "", "data": {
        "meter": meter,
        "month": _clean(mm.group(1)) if mm else None,
        "qcdl": _clean(bill.get("qcdl")),
        "qmdl": _clean(bill.get("qmdl")),
        "ydl": _clean(bill.get("ydl")),
        "ydje": _clean(bill.get("ydje")),
    }}


def fetch_meter_detail(meter: str, month: str | None = None,
                       timeout: float = 20.0) -> dict:
    """表号日用电明细（electricdetail，必须 POST）。只读。

    month: YYYY-MM（默认当前月）。
    返回 {'outcome', 'diag', 'data': {month, days: [{date, kwh, yuan}], total_kwh}}
    """
    meter = str(meter).strip()
    m = (month or datetime.now().strftime("%Y-%m")).strip()
    if not valid_month(m):
        return {"outcome": "invalid_arguments",
                "diag": "month 须为 YYYY-MM（2000..明年），收到 %r" % str(month)[:20],
                "data": None}
    rows = None
    last_diag = ""
    # 失效会话（非 200/errorApp）→ 淘汰并有限重建一次
    for _attempt in range(2):
        with _http_lock:
            s, err = _meter_session(meter, timeout)
            if err is not None:
                return {**err, "data": None}
            try:
                r = s.post(f"{BASE}/electricdetail",
                           data={"month": m, "comAddress": meter},
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Origin": BASE, "Referer": BASE + "/meter_main",
                                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                           timeout=timeout)
            except requests.RequestException as e:
                _drop_session(meter)
                return {"outcome": "network_error",
                        "diag": "%s: %s" % (type(e).__name__, e), "data": None}
        html = _decode(r)
        if r.status_code != 200 or "errorApp" in html:
            _drop_session(meter)
            last_diag = "明细页不可用 status=%s" % r.status_code
            continue
        rows = _detail_rows(html)
        break
    else:
        return {"outcome": "error", "diag": last_diag or "明细页不可用",
                "data": None}
    if rows is None:
        return {"outcome": "error", "diag": "日明细解析失败（可能改版）",
                "data": None}
    days = []
    for x in rows:
        if isinstance(x, dict) and str(x.get("ydl") or "").strip():
            days.append({"date": _clean(x.get("sj")),
                         "kwh": _clean(x.get("ydl")),
                         "yuan": _clean(x.get("ydje"))})
    total = None
    try:
        total = round(sum(float(d["kwh"]) for d in days), 2)
    except (TypeError, ValueError):
        pass
    return {"outcome": "ok", "diag": "", "data": {
        "meter": meter, "month": m, "days": days, "total_kwh": total,
    }}


def resolve_room_meter(building: str, floor: str, room: str,
                       area_label: str = "钱江湾学生公寓",
                       area_root_id: str | None = None,
                       timeout: float = 20.0) -> dict:
    """房间级联解析表号：楼栋→楼层→房间→绑房→comAddress。只读链路。

    building 如 "X幢"，floor 如 "X楼"（可传 "X"），room 如 "XXX"。
    area_root_id 为一级园区节点（对应园区的一级节点 id）；提供时只在该园区下查，
    未提供时扫描全部园区，但同名楼栋跨园区出现多个命中会拒绝（ambiguous），
    不做「取第一个」的猜测。

    返回 {'outcome': ok|not_found|ambiguous|error|parse_error, 'diag', 'meter',
          'root_id'}
    """
    try:
        s = _bootstrap(timeout)
    except requests.RequestException as e:
        return {"outcome": "error", "diag": "%s: %s" % (type(e).__name__, e),
                "meter": None}
    roots = [str(area_root_id).strip()] if area_root_id else list(_ROOT_IDS)
    # 在各一级节点下找目标楼栋（如 X幢）；跨园区必须唯一
    hits = []
    try:
        for rid in roots:
            r = s.post(f"{BASE}/getB", json={"bID": rid}, headers=_hj(),
                       timeout=timeout)
            data = _json_or_none(r)
            if data is None:
                return {"outcome": "parse_error",
                        "diag": "getB 非 JSON 响应（园区 %s）" % rid, "meter": None}
            hits.extend((rid, x) for x in
                        _unique_by_name(data.get("floor"), building))
    except requests.RequestException as e:
        return {"outcome": "error", "diag": "级联失败: %s" % e, "meter": None}
    if not hits:
        return {"outcome": "not_found",
                "diag": "在 %d 个园区节点中未找到楼栋 %r（园区 %s）"
                        % (len(roots), building, area_label),
                "meter": None}
    if len(hits) > 1:
        return {"outcome": "ambiguous",
                "diag": "园区中存在 %d 个同名楼栋 %r（%s）；请在 "
                        "data/dorm_meter.json 指定 area_root_id 后重试"
                        % (len(hits), building,
                           "/".join(sorted({rid for rid, _ in hits}))),
                "meter": None}
    root_id, bnode = hits[0]
    try:
        fname = floor if "楼" in floor else ("%s楼" % floor)
        r = s.post(f"{BASE}/getB", json={"fID": bnode["BuildingID"]},
                   headers=_hj(), timeout=timeout)
        data = _json_or_none(r)
        if data is None:
            return {"outcome": "parse_error",
                    "diag": "楼层列表非 JSON 响应", "meter": None}
        floors = data.get("room") or []
        fhits = _unique_by_name(floors, fname)
        if not fhits:
            return {"outcome": "not_found",
                    "diag": "楼栋下未找到楼层 %r（共 %d 层）" % (fname, len(floors)),
                    "meter": None}
        if len(fhits) > 1:
            return {"outcome": "ambiguous",
                    "diag": "楼栋内存在 %d 个同名楼层 %r" % (len(fhits), fname),
                    "meter": None}
        fnode = fhits[0]
        r = s.post(f"{BASE}/getB", json={"rID1": fnode["BuildingID"]},
                   headers=_hj(), timeout=timeout)
        data = _json_or_none(r)
        if data is None:
            return {"outcome": "parse_error",
                    "diag": "房间列表非 JSON 响应", "meter": None}
        rooms = data.get("room1") or []
        rkey = str(room).replace("房", "").strip()
        rhits = [x for x in rooms
                 if isinstance(x, dict)
                 and str(x.get("BuildingName") or "").strip().rstrip("房") == rkey
                 and x.get("BuildingID")]
        if not rhits:
            return {"outcome": "not_found",
                    "diag": "楼层下未找到房间 %r（共 %d 间）" % (rkey, len(rooms)),
                    "meter": None}
        if len(rhits) > 1:
            return {"outcome": "ambiguous",
                    "diag": "楼层内存在 %d 个同名房间 %r" % (len(rhits), rkey),
                    "meter": None}
        rnode = rhits[0]
        info = "%s|%s|%s|%s|%s" % (AREA_NAME, area_label, building, fname,
                                   rnode["BuildingName"])
        r = s.post(f"{BASE}/login",
                   json={"buildingInfo": info, "buildingID": rnode["BuildingID"]},
                   headers=_hj(), timeout=timeout)
        login = _json_or_none(r)
        if login is None:
            return {"outcome": "parse_error",
                    "diag": "绑房响应非 JSON", "meter": None}
        href = login.get("href")
        if not href:
            return {"outcome": "error",
                    "diag": "绑房未返回 href（锁房或级联路径不符）", "meter": None}
        r = s.get(f"{BASE}/{str(href).lstrip('/')}",
                  headers={"Referer": BASE + "/login", "Accept": "text/html"},
                  allow_redirects=False, timeout=timeout)
        html = _decode(r)
        found = sorted(set(_COMADDR_RE.findall(html)) |
                       set(_COMADDR_RE2.findall(html)))
        if not found:
            return {"outcome": "error", "diag": "绑房页未解析出表号",
                    "meter": None}
        return {"outcome": "ok", "diag": "", "meter": found[0],
                "root_id": root_id}
    except (requests.RequestException, json.JSONDecodeError) as e:
        return {"outcome": "error", "diag": "级联失败: %s" % e, "meter": None}
