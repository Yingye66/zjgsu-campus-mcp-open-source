# -*- coding: utf-8 -*-
"""查询课表可见课程的已知学分小计。

无参数时读取教务页面 selected 当前学年/学期，不按本机月份猜测。
也可显式指定：python query_semester_credits.py 2026 3
此结果不是官方修读总学分或已取得学分；缺值、冲突和覆盖缺口会单独提示。
导入模块不会初始化缓存或读取本地会话；执行 main 时才使用本机会话。
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from src.adapters.campus.jwweb import fetch_current_term
from src.auth.session import get_active
from src.domain.models import MODULE_TIMETABLE_WEB, SOURCE_JWGLXT
from src.paths import default_data_dir
from src.service.jwweb_service import JWWebService
from src.storage.store import SnapshotStore


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xnm", nargs="?", help="四位学年，如 2026 表示 2026-2027")
    parser.add_argument("xqm", nargs="?", choices=("3", "12", "16"),
                        help="3=第一学期，12=第二学期，16=短学期")
    args = parser.parse_args(argv)
    if (args.xnm is None) != (args.xqm is None):
        parser.error("xnm 和 xqm 必须同时提供；都省略时读取学校当前学期")
    if args.xnm is not None and re.fullmatch(r"[0-9]{4}", args.xnm) is None:
        parser.error("xnm 必须为四位 ASCII 数字学年")
    return args


def _issue_codes(issues):
    """只输出具名错误码，不把来源诊断或响应正文复制到终端。"""
    return sorted({issue["code"] for issue in issues
                   if isinstance(issue, dict) and isinstance(issue.get("code"), str)
                   and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", issue["code"])})


def _complete(wrapper):
    snapshots = wrapper.get("coverage", {}).get("source_snapshots", [])
    return (wrapper.get("status") == "ok" and bool(snapshots)
            and all(s.get("complete") is True and not s.get("missing_ranges")
                    for s in snapshots))


def _collect_pages(service, xnm, xqm):
    """首页仅联网一次；后续调用始终携带本地快照游标。"""
    result = service.get_timetable_web(xnm=xnm, xqm=xqm, limit=500)
    issues = list(result.get("issues") or [])
    if result.get("status") not in ("ok", "partial"):
        return None, issues

    items = list(result.get("items") or [])
    complete = _complete(result)
    partial = result["status"] == "partial"
    pagination = result.get("pagination") or {}
    snapshot_id = pagination.get("result_snapshot_id")
    has_more = pagination.get("has_more") is True
    seen_cursors = set()
    stopped = False
    while has_more:
        cursor = pagination.get("next_cursor")
        if (not isinstance(cursor, str) or not cursor or cursor in seen_cursors
                or not isinstance(snapshot_id, str) or not snapshot_id):
            issues.append({"code": "invalid_pagination"})
            stopped = True
            break
        seen_cursors.add(cursor)
        page = service.get_timetable_web(xnm=xnm, xqm=xqm, cursor=cursor, limit=500)
        issues.extend(page.get("issues") or [])
        if page.get("status") not in ("ok", "partial"):
            issues.append({"code": "pagination_interrupted"})
            stopped = True
            break
        next_pagination = page.get("pagination") or {}
        if next_pagination.get("result_snapshot_id") != snapshot_id:
            issues.append({"code": "snapshot_changed"})
            stopped = True
            break
        items.extend(page.get("items") or [])
        complete = complete and _complete(page)
        partial = partial or page["status"] == "partial"
        pagination = next_pagination
        has_more = pagination.get("has_more") is True
    return {"items": items, "complete": complete and not stopped,
            "has_more": has_more, "partial": partial, "stopped": stopped}, issues


def main(argv=None) -> int:
    args = _arguments(argv)
    data_dir = Path(default_data_dir())
    if not get_active(data_dir):
        print("本地无会话：请先运行 python -m src.auth.login_cli init")
        return 2

    service = JWWebService(SnapshotStore(data_dir / "cache.sqlite3"), data_dir)
    xnm, xqm = args.xnm, args.xqm
    if xnm is None:
        _account, _cookies, transport, error = service._jwweb_ctx(
            MODULE_TIMETABLE_WEB, SOURCE_JWGLXT, "current-term")
        if error is not None:
            print("当前学期读取失败：本地教务会话不可用，请重新登录。")
            return 2
        term = fetch_current_term(transport)
        xnm, xqm = term.get("term"), term.get("term_xqm")
        if (term.get("outcome") != "ok" or not isinstance(xnm, str)
                or re.fullmatch(r"[0-9]{4}", xnm) is None
                or xqm not in ("3", "12", "16")):
            print("当前学期未能从学校 selected 项确定；请显式提供 xnm xqm，不按月份猜测。")
            return 1

    result, issues = _collect_pages(service, xnm, xqm)
    if result is None:
        codes = _issue_codes(issues)
        print("课表读取失败：" + ("、".join(codes) or "unavailable"))
        return 2 if "auth_required" in codes else 1

    from src.service.semester_credits import summarize_timetable_credits
    summary = summarize_timetable_credits(
        result["items"], complete=result["complete"],
        has_more=result["has_more"], issues=issues)
    print(f"== {xnm}-{int(xnm) + 1} 学年，学期码 {xqm} ==")
    print(f"课表可见课程的已知学分小计：{summary['known_credit_sum']} 学分"
          f"（{summary['visible_course_count']} 门可识别课程）")
    print("口径：仅课表可见课程；不能据此认定官方修读总学分或已取得学分。")
    if summary["unknown_credit_courses"]:
        print("学分缺失或无效：" + "、".join(summary["unknown_credit_courses"]))
    if summary["conflicting_courses"]:
        print("学分冲突：" + "、".join(summary["conflicting_courses"]))
    if summary["unidentified_row_count"]:
        print(f"无法识别课程的记录：{summary['unidentified_row_count']} 条（未计入小计）")
    if result["partial"]:
        print("读取状态：partial；仅展示当前可取得的观测或既有快照，存在缺口。")
    if not summary["source_complete"]:
        print("来源完整性未证明或存在缺口；此小计不能视为全部课程学分。")
    codes = _issue_codes(issues)
    if codes:
        print("提示码：" + "、".join(codes))
    for note in summary["notes"]:
        print("提示：" + str(note))
    return 1 if result["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
