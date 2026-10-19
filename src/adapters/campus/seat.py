# -*- coding: utf-8 -*-
"""Experimental seat adapter (read-only); see docs/release-status.md.

The upstream site only serves plaintext HTTP (HTTPS verified unusable), so the
service layer refuses to send the session Cookie unless explicitly opted in
via ZJGSU_SEAT_ALLOW_INSECURE_HTTP=1 (debugging only). Manual entry through
the official library menu is recommended. No booking/reservation/cancellation
operations are provided.
"""
from __future__ import annotations

import os
import re

import requests

SEAT_BASE = "http://wechatseat.zjgsu.edu.cn"
_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
       "Chrome/86.0 Mobile Safari/537.36 MicroMessenger/8.0.49.2600")
_INSECURE_ENV = "ZJGSU_SEAT_ALLOW_INSECURE_HTTP"

MANUAL_GUIDE = ("微信「浙商大图书馆助手」→ 选座系统 → 选择楼层查看实时座位图；"
                "或到图书馆一楼大厅大屏现场查看。")

_ROW_RE = re.compile(
    r'data-url="/index\.php/reserve/layout/libid=(\d+)\.html[^"]*"[^>]*>\s*'
    r'<h4[^>]*>([^<]+?)\s*\(([^)]+)\)\s*<span[^>]*>(\d+)/(\d+)</span>')
_CELL_RE = re.compile(r'grid_cell\s+(grid_\d)"')


def insecure_http_allowed() -> bool:
    """本机排障开关：默认 False（不明文发送会话 Cookie）。"""
    return os.environ.get(_INSECURE_ENV, "").strip() == "1"


def _insecure_block() -> dict:
    return {
        "outcome": "insecure_transport",
        "diag": "选座源仅提供 HTTP 明文入口（443 实测无响应，HTTPS 不可用）；"
                "为避免在明文链路发送会话 Cookie，自动查询默认关闭。"
                "如需本机排障，显式设置 ZJGSU_SEAT_ALLOW_INSECURE_HTTP=1 后重试。",
        "areas": [],
    }


def _headers(cookie_header: str) -> dict:
    return {"User-Agent": _UA, "Cookie": cookie_header,
            "Referer": SEAT_BASE + "/index.php/reserve/index.html"}


def _is_session_redirect(resp: requests.Response) -> bool:
    loc = resp.headers.get("Location", "")
    return resp.status_code in (301, 302, 303, 307) and \
        ("weixin" in loc or "oauth" in loc)


def fetch_areas(cookie_header: str, timeout: int = 20) -> dict:
    """拉取全部区域实时余座（单请求）。

    返回 {'outcome': ok|not_configured|insecure_transport|session_expired|
    network_error|http_error|parse_error, 'diag', 'areas'}
    areas: [{lib_id, name, floor, free, total}]
    """
    if not cookie_header or "wechatSESS" not in cookie_header:
        return {"outcome": "not_configured", "diag": "未导入选座会话 Cookie"
                "（login_cli seat-cookie 或真机收割脚本）", "areas": []}
    if not insecure_http_allowed():
        return _insecure_block()
    try:
        resp = requests.get(
            SEAT_BASE + "/index.php/reserve/index.html",
            params={"f": "wechat"}, headers=_headers(cookie_header),
            timeout=timeout, allow_redirects=False)
    except Exception as e:
        return {"outcome": "network_error",
                "diag": "%s: %s" % (type(e).__name__, e), "areas": []}
    if _is_session_redirect(resp):
        return {"outcome": "session_expired",
                "diag": "会话已失效（303→微信 OAuth），请重新收割 Cookie",
                "areas": []}
    if resp.status_code != 200:
        return {"outcome": "http_error", "diag": "HTTP %s" % resp.status_code,
                "areas": []}
    text = resp.content.decode("utf-8", "replace")
    if "座位预订" not in text and "list-group" not in text:
        return {"outcome": "parse_error",
                "diag": "页面结构不含座位列表（%dB）" % len(text), "areas": []}
    areas = []
    for m in _ROW_RE.finditer(text):
        libid, name, floor, free, total = m.groups()
        areas.append({"lib_id": int(libid), "name": name.strip(),
                      "floor": floor.strip().replace("楼", ""),
                      "free": int(free), "total": int(total)})
    if not areas:
        return {"outcome": "parse_error", "diag": "0 行解析（页面可能改版）",
                "areas": []}
    return {"outcome": "ok", "diag": "", "areas": areas}


def fetch_layout_counts(cookie_header: str, lib_id: int,
                        timeout: int = 25) -> dict:
    """拉取某区域座位图并按状态统计（只读）。

    返回 {'outcome', 'diag', 'counts': {grid_1: n, ...}, 'cells': 总格数}
    grid_1=空闲；其余类为占用/家具混合（页面 CSS 类，语义见取证记录）。
    """
    if not insecure_http_allowed():
        return {"outcome": "insecure_transport", "diag": _insecure_block()["diag"]}
    try:
        resp = requests.get(
            SEAT_BASE + "/index.php/reserve/layout/libid=%d.html" % lib_id,
            params={}, headers=_headers(cookie_header), timeout=timeout,
            allow_redirects=False)
    except Exception as e:
        return {"outcome": "network_error",
                "diag": "%s: %s" % (type(e).__name__, e)}
    if _is_session_redirect(resp):
        return {"outcome": "session_expired", "diag": "会话失效"}
    if resp.status_code != 200:
        return {"outcome": "http_error", "diag": "HTTP %s" % resp.status_code}
    text = resp.content.decode("utf-8", "replace")
    cells = _CELL_RE.findall(text)
    counts: dict[str, int] = {}
    for g in cells:
        counts[g] = counts.get(g, 0) + 1
    return {"outcome": "ok", "diag": "", "counts": counts, "cells": len(cells)}


def free_rate(free: int, total: int) -> float | None:
    """空位率；总数为 0 时返回 None（不参与除法）。"""
    if not isinstance(total, int) or total <= 0:
        return None
    return round(free / total, 3)


def aggregate_by_floor(areas: list) -> dict:
    """区域列表 → 楼层聚合（free/total + 空位率），含排序建议。"""
    floors: dict[str, dict] = {}
    for a in areas:
        f = floors.setdefault(a["floor"], {"free": 0, "total": 0, "areas": []})
        f["free"] += a["free"]
        f["total"] += a["total"]
        f["areas"].append(a)
    out = {"by_floor": floors, "rank_by_free": [], "rank_by_rate": []}
    flat = sorted(areas, key=lambda a: -a["free"])
    out["rank_by_free"] = [{"name": a["name"], "floor": a["floor"],
                            "free": a["free"], "total": a["total"],
                            "free_rate": free_rate(a["free"], a["total"])}
                           for a in flat]
    out["rank_by_rate"] = sorted(
        out["rank_by_free"],
        key=lambda x: (x["free_rate"] is not None, x["free_rate"] or 0.0),
        reverse=True)
    return out


def fetch_seat_summary(cookie_header: str, with_detail: bool = False) -> dict:
    """对外入口：楼层占用汇总（+可选区域座位图统计）。"""
    res = fetch_areas(cookie_header)
    if res["outcome"] != "ok":
        return {"outcome": res["outcome"], "diag": res["diag"], "data": None}
    agg = aggregate_by_floor(res["areas"])
    data = {"areas": res["areas"], **agg}
    if with_detail and res["areas"]:
        det = fetch_layout_counts(cookie_header, res["areas"][0]["lib_id"])
        data["detail_sample"] = {"lib_id": res["areas"][0]["lib_id"],
                                 "outcome": det["outcome"],
                                 "counts": det.get("counts")}
    return {"outcome": "ok", "diag": "", "data": data}
