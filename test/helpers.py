# -*- coding: utf-8 -*-
"""测试公共设施：假传输、可控时钟、会话种子、数据构造、临时目录。

临时目录基座：默认使用项目内 `validation/test-temp`（在受限环境中系统 TEMP 可能不可写，
经 CAMPUS_TEST_TMP_BASE 可覆盖；该目录已被 .gitignore 排除）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.auth.session import save_session, set_active  # noqa: E402

TZ = timezone(timedelta(hours=8))


def temp_dir() -> tempfile.TemporaryDirectory:
    """创建临时目录：优先项目内基座（受限环境亦可用），可用 CAMPUS_TEST_TMP_BASE 覆盖。"""
    base = os.environ.get("CAMPUS_TEST_TMP_BASE") or str(PROJECT_ROOT / "validation" / "test-temp")
    Path(base).mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)


class FakeTransport:
    """脚本化响应队列 + 调用记录（替换真实 HTTP，用于合成验收）。"""

    def __init__(self, responses: list | None = None):
        self.queue = list(responses or [])
        self.calls: list[dict] = []

    def push(self, resp: dict):
        self.queue.append(resp)

    def post_form(self, url: str, form: dict | None = None,
                  headers: dict | None = None, timeout: int = 20) -> dict:
        self.calls.append({"url": url, "form": dict(form or {}),
                           "headers": dict(headers or {})})
        if not self.queue:
            raise AssertionError("FakeTransport 响应队列为空（测试脚本缺少预期响应）")
        return self.queue.pop(0)


class Clock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 10, 13, 14, 0, 0, tzinfo=TZ)

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def seed_session(data_dir: Path, account: str, user_type: str = "2",
                 created_at: str = "2026-10-13T14:00:00+08:00") -> None:
    save_session(data_dir, account, {
        "schema": 1,
        "account_scope_id": account,
        "client_id": "TEST_CLIENT",
        "zjgsu_token": "TEST_TOKEN_%s" % account,
        "user_type": user_type,
        "created_at": created_at,
    })
    set_active(data_dir, account)


def course_item(**over) -> dict:
    base = {
        "kcmc": "课程A", "xqj": 1, "ksj": "1", "jsj": "2", "jsmc": "A101",
        "xn": "2026-2027", "xq": "1", "zc": 5, "skrq": "10-12",
        "nyr": "2026-10-12", "kcdm": "C001", "kclx": "本科生", "jsbh": "00001",
        "id": "ID-1",
    }
    base.update(over)
    return base


def course_ok(items: list[dict]) -> dict:
    return {"kind": "response", "status": 200,
            "text": json.dumps({"data": {"code": "0", "list": items}}, ensure_ascii=False)}


def course_code(code: str, items: list[dict] | None = None) -> dict:
    body: dict = {"data": {"code": code}}
    if items is not None:
        body["data"]["list"] = items
    return {"kind": "response", "status": 200, "text": json.dumps(body, ensure_ascii=False)}


def http_500() -> dict:
    return {"kind": "response", "status": 500, "text": ""}


def network_err() -> dict:
    return {"kind": "network_error", "err": "synthetic timeout"}


def not_json() -> dict:
    return {"kind": "response", "status": 200, "text": "<html>login</html>"}


# ================= 宣讲会（jyw）夹具 =================

def event_item(**over) -> dict:
    """与真实响应同构的合成条目（字段名取自 2026-10-13 公开源实测）。"""
    base = {
        "xjhid": "240000000000000001", "xjhzt": "示例公司校园宣讲会", "dwmc": "示例公司",
        "jbrq": "2026-10-14", "jbsjdmc": "14:00-17:00", "jbxqmc": "下沙校区",
        "cdmc": "B108", "cddz": "教室需借用成功后才能确定具体位置",
        "jbxs": "01", "jbxsmc": "校内宣讲", "shzt": "03", "xzyjmc": "民营企业",
        "hyyjmc": "制造业", "rsgmmc": "1000-4999人", "zwmcs": "研发,财务",
        "djs": 100, "ontop": "否", "xsxjhwz": None,
    }
    base.update(over)
    return base


def events_resp(items: list[dict], total=None, pages=1, is_last=True,
                page_num=1, page_size=50, code=200) -> dict:
    """宣讲会列表响应（与真实结构同构；total 为字符串）。"""
    t = total if total is not None else len(items)
    return {"kind": "response", "status": 200, "text": json.dumps({
        "code": code,
        "data": {"total": str(t), "list": items, "pageNum": page_num,
                 "pageSize": page_size, "pages": pages,
                 "isFirstPage": page_num == 1, "isLastPage": is_last,
                 "hasNextPage": not is_last},
        "message": "操作成功" if code == 200 else "操作异常！",
    }, ensure_ascii=False)}
