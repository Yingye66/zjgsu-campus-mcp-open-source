# -*- coding: utf-8 -*-
"""宣讲会抓取与映射（公开来源；逐日查询 + 分页读完 + 字段校验）。

依据：validation/career/取证记录.md（全部语义经 2026-10-13 公开源实测）。
关键设计（对应交接文档第 2.9 节与信息覆盖清单第 7 节）：
- 按举办日精确筛选：form 参数 `jbrqs=yyyy-MM-dd`（`jbrqe` 区间不被支持 → 逐日查询）；
- 单日分页：`/all/{page}/{size}`，读至 `isLastPage` 或达到页数上限（有界请求）；
- 匿名可读（无会话）；不发送任何账号信息；
- 完整性：每日本身有 total/pages/isLastPage 证据 → 全部读完标记 complete=True，
  否则记 missing_ranges；日期不在请求日内或字段不合规的条目按规则处置（见下）。
- 状态（进行中/未开始/已结束/已取消）在**响应组装时**按当前时间计算（缓存里只存原始字段），
  与页面前端同构（allxjh.html L1222–1236）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone

from .classify import classify_events

BASE = "https://jyw.zjgsu.edu.cn"
LIST_PATH = "/career/xjhxx/search/all"
TZ = timezone(timedelta(hours=8))  # 来源时间为中国本地时间（无夏令时；固定 +08:00）
MAX_PAGES_PER_DAY = 5      # 每日最多读 5 页（页幅 50 → 250 条/日；超出记缺失）
PAGE_SIZE = 50

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE + "/career/xjhxx/allxjh",
}

_RE_FULL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

EVENT_TYPE_XJH = "宣讲会"
SOURCE_JYW_XJH = "src.jyw-xjh"


@dataclass
class DayResult:
    day: str
    outcome: str                 # ok / ok_empty / 各种失败
    items: list[dict] = field(default_factory=list)   # 映射后的条目
    complete: bool = False       # 该日是否读完（含空）
    truncated_pages: int = 0     # 因页数上限未读的页数
    diag: str = ""


def _to_bool(v):
    """isLastPage 等结束标志的严格判读；无法判读返回 None（不猜）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "01"):
            return True
        if s in ("false", "0", "00"):
            return False
    return None


def _page_consistency(data: dict, page: int) -> str | None:
    """单页分页元数据校验（R14，交接 2.11/2.13）。返回矛盾原因；None 表示通过。

    校验：pageNum 存在且为整数（bool 不算、字符串拒绝）并与请求页一致、
    total 不超过 pages×页幅、isLastPage 与 pages 不矛盾。
    累计条数与重复 ID 由 fetch_day 在采用前核对。
    """
    if "pageNum" not in data:
        return "缺少 pageNum——分页判据不完整"
    pn = data["pageNum"]
    if isinstance(pn, bool) or not isinstance(pn, int):
        return "pageNum=%r 类型不符（须为整数）" % (pn,)
    if pn != page:
        return "响应 pageNum=%s 与请求页 %d 不一致" % (pn, page)
    total = int(str(data.get("total", "0")))
    pages = int(str(data.get("pages", "0")))
    if total > pages * PAGE_SIZE:
        return "total=%d 超过 pages=%d × 页幅 %d——元数据矛盾" % (total, pages, PAGE_SIZE)
    last = _to_bool(data.get("isLastPage"))
    if last is None:
        return "isLastPage=%r 无法判读" % (data.get("isLastPage"),)
    if last and page < pages:
        return "isLastPage=true 但 pages=%d 大于当前页 %d" % (pages, page)
    return None


def _parse_time_range(raw) -> tuple[str | None, str | None, str | None]:
    """'14:00-17:00' -> ('14:00','17:00',raw)；单值 '14:00' -> ('14:00',None,raw)
    （R15：单时刻只能确定开始，结束时间保持 None，不猜）；不可解析 -> (None,None,raw)。"""
    if not isinstance(raw, str) or raw.strip() == "":
        return (None, None, None)
    s = raw.strip()
    parts = [p.strip() for p in s.split("-") if p.strip() != ""]
    if len(parts) == 1 and _RE_TIME.match(parts[0]):
        return (parts[0], None, s)
    if len(parts) == 2 and _RE_TIME.match(parts[0]) and _RE_TIME.match(parts[1]):
        return (parts[0], parts[1], s)
    return (None, None, s)


def map_event(raw: dict, day: str) -> dict | None:
    """原始条目 -> 存储条目；必填不合规返回 None（调用方整日拒绝）。

    必填：xjhid（非空）、xjhzt（非空）、dwmc（非空）、jbrq（合法日期且 == 请求日）。
    可选字段缺失/空值容错为 None；'是'/'否' 归一为布尔。
    """
    eid = raw.get("xjhid")
    title = raw.get("xjhzt")
    company = raw.get("dwmc")
    jbrq = raw.get("jbrq")
    if not (isinstance(eid, str) and eid.strip()):
        return None
    if not (isinstance(title, str) and title.strip()):
        return None
    if not (isinstance(company, str) and company.strip()):
        return None
    if not (isinstance(jbrq, str) and _RE_FULL_DATE.match(jbrq.strip())):
        return None
    if jbrq.strip() != day:
        return None  # 防御性过滤：与请求日不一致的条目丢弃（不应出现）

    start_t, end_t, time_range = _parse_time_range(raw.get("jbsjdmc"))
    jbxs = raw.get("jbxs")
    location = raw.get("cdmc") if jbxs == "01" else raw.get("xsxjhwz")

    def s(key):
        v = raw.get(key)
        if v is None or v == "":
            return None
        return str(v)

    return {
        "event_id": eid.strip(),
        "title": title.strip(),
        "company": company.strip(),
        "event_type": EVENT_TYPE_XJH,
        "date": day,
        "time_range": time_range,
        "start_time": start_t,
        "end_time": end_t,
        "campus": s("jbxqmc"),
        "location": (str(location).strip() if location not in (None, "") else None),
        "format": s("jbxsmc"),
        "cancelled": (raw.get("shzt") == "05"),
        "company_type": s("xzyjmc"),
        "industry": s("hyyjmc"),
        "company_size": s("rsgmmc"),
        "positions": s("zwmcs"),
        "view_count": raw.get("djs") if isinstance(raw.get("djs"), int) else None,
        "on_top": (raw.get("ontop") == "是"),
        "url": "%s/career/xjhxx/view/%s" % (BASE, eid.strip()),
        "source_id": SOURCE_JYW_XJH,
    }


def derive_status(entry: dict, now: datetime) -> str | None:
    """按当前时间计算状态：已取消 / 进行中 / 未开始 / 已结束；无法计算 -> None。

    R15（交接 2.11/2.13）：
    - 仅有开始时刻（无结束时间）时只能确定「未开始」或「已开始（结束时间未知）」；
    - 旧缓存可能带有上一版解析器补猜的 end_time——优先从原始 time_range 重解析
      （time_range 是来源原文字段，end_time 是解析产物，以原文为准）；
    - 跨日区间（结束时刻早于开始时刻，如 23:00-01:00）结束日无法确定：
      不补猜结束日，开始后一律「已开始（结束时间未知）」。
    来源时间为中国本地时间：比较前统一附加 +08:00（now 无时区时按 +08:00 解释）。
    """
    if entry.get("cancelled"):
        return "已取消"
    day = entry.get("date")
    st, et = _effective_times(entry)
    if not st or not day:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    try:
        h, m = st.split(":")
        start_dt = datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(),
                                    dtime(int(h), int(m)), TZ)
    except Exception:
        return None
    if now < start_dt:
        return "未开始"
    end_dt = None
    if et:
        try:
            h2, m2 = et.split(":")
            cand = datetime.combine(start_dt.date(), dtime(int(h2), int(m2)), TZ)
            if cand > start_dt:  # 同日内合法；早于开始 = 跨日/矛盾 → 不猜
                end_dt = cand
        except Exception:
            end_dt = None
    if end_dt is None:
        return "已开始（结束时间未知）"
    if now <= end_dt:
        return "进行中"
    return "已结束"


def _effective_times(entry: dict) -> tuple[str | None, str | None]:
    """返回 (start_time, end_time)：优先从原始 time_range 重解析，
    time_range 不可解析时回退缓存字段（R15 旧缓存残余）。"""
    st, et, _ = _parse_time_range(entry.get("time_range"))
    if st is None:
        st = entry.get("start_time")
        et = entry.get("end_time")
    return st, et


def fetch_day(transport, day: str, keyword: str | None = None) -> DayResult:
    """抓取单日全部页（有界）。返回 DayResult（含完整性与缺口信息）。

    R14（交接 2.11）：每页先做分页元数据一致性校验与重复 ID 检查；
    矛盾响应返回 parser_mismatch——不采用、不成为成功快照。
    """
    items: list[dict] = []
    seen_ids: set = set()
    page = 1
    while True:
        url = "%s%s/%d/%d" % (BASE, LIST_PATH, page, PAGE_SIZE)
        form = {"jbrqs": day}
        if keyword:
            form["dwmc"] = keyword
        r = transport.post_form(url, form=form, headers=HEADERS)
        outcome, data, diag = classify_events(r.get("kind"), r.get("status", 0), r.get("text", ""))
        if outcome not in ("ok", "ok_empty"):
            return DayResult(day=day, outcome=outcome, items=[],
                             complete=False, diag="%s 查询失败：%s" % (day, diag))
        problem = _page_consistency(data, page)
        if problem:
            return DayResult(day=day, outcome="parser_mismatch", items=[],
                             complete=False,
                             diag="%s 分页元数据矛盾：%s" % (day, problem))
        lst = data.get("list", [])
        bad = 0
        page_items: list[dict] = []
        for raw in lst:
            mapped = map_event(raw, day)
            if mapped is None:
                bad += 1
            else:
                page_items.append(mapped)
        if bad:
            # 必填不合规/日期不符：整日拒绝（不产出半成品）
            return DayResult(day=day, outcome="schema_invalid", items=[],
                             complete=False,
                             diag="%s schema 校验未通过（%d 条不合规）" % (day, bad))
        # R14（交接 2.13）：同页与跨页重复 ID 均拒绝
        ids = [m["event_id"] for m in page_items]
        if len(set(ids)) != len(ids):
            return DayResult(day=day, outcome="parser_mismatch", items=[],
                             complete=False,
                             diag="%s 同页出现重复 xjhid——分页可能重复" % day)
        dup = [i for i in ids if i in seen_ids]
        if dup:
            return DayResult(day=day, outcome="parser_mismatch", items=[],
                             complete=False,
                             diag="%s 分页出现重复 xjhid（%s）——分页可能重复" % (day, dup[0]))
        seen_ids.update(ids)
        items.extend(page_items)
        pages = int(str(data.get("pages", 0)))
        total = int(str(data.get("total", "0")))
        is_last = _to_bool(data.get("isLastPage"))
        if is_last or pages <= page:
            # R14（交接 2.13）：结束页无论 last 真假，都核对累计唯一条数与 total
            if total != len(items):
                return DayResult(day=day, outcome="parser_mismatch", items=[],
                                 complete=False,
                                 diag="%s 声明总数 total=%d 与实际读取 %d 条不一致（读至第 %d 页）"
                                      % (day, total, len(items), page))
            return DayResult(day=day, outcome="ok_empty" if not items else "ok",
                             items=items, complete=True, diag="%s 读完（pages=%d）" % (day, pages))
        page += 1
        if page > MAX_PAGES_PER_DAY:
            return DayResult(day=day, outcome="ok_empty" if not items else "ok",
                             items=items, complete=False,
                             truncated_pages=pages - (page - 1),
                             diag="%s 仅读前 %d 页（共 %d 页）" % (day, MAX_PAGES_PER_DAY, pages))


@dataclass
class EventsFetch:
    outcome: str
    entries: list[dict] | None
    complete: bool
    missing_ranges: list[str]
    diag: str
    days_read: int = 0
    failed_day: str | None = None        # 首个失败日（None=全部成功）
    failure_outcome: str | None = None   # 失败日的 outcome（供 service 具名）
    failure_diag: str = ""


def fetch_events_range(transport, start_date: str, end_date: str,
                       keyword: str | None = None, days: list[str] | None = None) -> EventsFetch:
    """逐日抓取 [start_date, end_date]（含端点；起始≤结束）。

    days：显式日期列表（测试/内部使用）；缺省由 start/end 展开（调用方已校验范围）。
    某日失败时保留已读日的数据（entries 非 None + failed_day）——由调用方决定
    返回 partial（已读部分 + 缺口）还是 unavailable；不丢弃已取数据。
    """
    if days is None:
        from datetime import date, timedelta
        d1 = date.fromisoformat(start_date)
        d2 = date.fromisoformat(end_date)
        days = [(d1 + timedelta(days=i)).isoformat() for i in range((d2 - d1).days + 1)]

    all_items: list[dict] = []
    missing: list[str] = []
    complete = True
    for idx, day in enumerate(days):
        dr = fetch_day(transport, day, keyword=keyword)
        if dr.outcome not in ("ok", "ok_empty"):
            # R14/缺口（交接 2.13）：缺口 = 此前限页日 + 失败日 + 后续未尝试日
            rest = days[idx + 1:]
            gaps = (list(missing) + [day]
                    + (["%s（未尝试）" % d for d in rest] if rest else []))
            return EventsFetch(
                outcome="ok" if all_items else dr.outcome,
                entries=all_items if all_items else None,
                complete=False,
                missing_ranges=gaps,
                diag="%s 读取失败：%s" % (day, dr.diag),
                days_read=idx + 1,
                failed_day=day, failure_outcome=dr.outcome, failure_diag=dr.diag)
        all_items.extend(dr.items)
        if not dr.complete:
            complete = False
            missing.append("%s（仅读前 %d 页）" % (day, MAX_PAGES_PER_DAY))
    all_items.sort(key=lambda x: (x["date"], x.get("start_time") or "", x.get("company") or ""))
    return EventsFetch(
        outcome="ok" if all_items else "ok_empty",
        entries=all_items,
        complete=complete,
        missing_ranges=missing,
        diag="%d 日读取完成（%d 条；完整性%s）" % (
            len(days), len(all_items), "已证" if complete else "部分/待证"),
        days_read=len(days))
