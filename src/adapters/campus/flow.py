# -*- coding: utf-8 -*-
"""泛微 Ecology OA（flow.zjgsu.edu.cn）读取适配器：本人流程申请列表（F11）。

取证 2026-10-16（validation/flow/取证记录.md，真实数据冒烟）：
- CAS 链（关键差异）：flow 侧的 CAS service 是 /login/Login.jsp（带 :443 端口，
  见 Login.jsp 302 Location 原文）。此前对 static4form/index.html 直接发 ticket
  只换到**匿名** ecology 会话——API 一律返回 {"errorCode":"002","msg":"登录信息超时"}。
  正确链路：GET /login/Login.jsp → 302 CAS?service=login.jsp → 302 回
  Login.jsp?ticket=... → 校验后发放 loginidweaver/languageidweaver（登录态）。
- 列表三段式（Ecology9 移动流程中心，getTabs 实测口径，ismobile=1）：
  1) GET /api/workflow/mobile/getListResult?ismobile=1&viewScope=<scope>&menuid=10
     → {"datas": "<sessionkey>", ...}（datas 即列表 sessionkey）
  2) POST /api/ec/dev/table/datas（form：dataKey=<sessionkey>&current=<页>&pageSize=<每页>）
     → {columns:[...], datas:[行], ...}（columns 为视图列定义，行含大量 span 冗余字段）
- 行字段（真实样本核实）：requestid（流程 id）、requestname（标题原文）、
  createdate（创建日期 yyyy-MM-dd）、workflowid（工作流数字 id）。
  本视图不回工作流名称/当前节点/审批状态——不猜名、不造状态。
- viewScope：mine（我的请求，工具默认）/ doing（待办事宜）/ done（已办事宜）。

纪律：只读（列表查询）；同意/转交/撤回/退回等写操作全部不做；
逐跳 host 白名单（uia/flow，R71 同款）；秘密（ticket/cookie 值）不进任何输出。
"""
from __future__ import annotations

import re
import urllib.parse

FLOW_BASE = "https://flow.zjgsu.edu.cn"
FLOW_LOGIN = FLOW_BASE + "/login/Login.jsp"
UIA_CAS = "https://uia.zjgsu.edu.cn/cas/login"
# 逐跳白名单（R71 同款：每跳校验 host，禁未校验自动跟随）
FLOW_ALLOWED_HOSTS = ("uia.zjgsu.edu.cn", "flow.zjgsu.edu.cn")

VIEW_SCOPES = ("mine", "doing", "done")

UA = "Mozilla/5.0"

# 白名单投影（来源行字段 → 输出字段；未知/嵌套不透传）
_ROW_FIELDS = (
    ("requestid", "request_id"),
    ("requestname", "request_name"),
    ("createdate", "create_date"),
    ("workflowid", "workflow_id"),
)


def _get_follow(sess, url: str, max_hops: int = 8):
    """白名单逐跳跟随重定向（返回最终 response 与失败原因）。

    B03 反馈 P1：每跳校验 https + host 白名单（防降级/出域）。"""
    current = url
    resp = None
    for _ in range(max_hops):
        try:
            pu = urllib.parse.urlparse(current)
            port = pu.port  # :invalid 端口在访问时抛 ValueError——受控
        except ValueError:
            return None, "重定向 URL 非法（端口/格式无效）"
        if pu.hostname is None or pu.hostname.lower() not in FLOW_ALLOWED_HOSTS:
            return None, "重定向 host 出域（%s）" % (pu.hostname or "无")
        if pu.scheme != "https":
            return None, "重定向降级为非 HTTPS（%s）" % pu.scheme
        if port not in (None, 443):
            return None, "重定向端口非预期（%s:%s）" % (pu.hostname, port)
        resp = sess.get(current, timeout=20, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location") or ""
            if not loc:
                break
            current = loc if loc.startswith("http") else (
                pu.scheme + "://" + pu.netloc + loc)
            continue
        break
    return resp, None


def fetch_my_requests(jwweb_cookies, view_scope: str = "mine",
                      current: int = 1, page_size: int = 20):
    """F11：本人流程列表（viewScope ∈ mine/doing/done）。

    返回 {'outcome': ok/ok_empty/http_error/parse_error/upstream_error/network_error,
          'items': [白名单投影行], 'diag': str}。
    CAS 未登录（无 ticket 交换成功）→ upstream_error（调用方按 auth 语义呈现）。
    """
    import requests
    if view_scope not in VIEW_SCOPES:
        return {"outcome": "upstream_error", "items": None,
                "diag": "未知 viewScope（%s）" % view_scope}
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    for c in (jwweb_cookies or []):
        try:
            # B03 反馈 P1：保留 secure/expires（不丢安全属性）
            sess.cookies.set(c["name"], c["value"], domain=c["domain"],
                             path=c.get("path") or "/",
                             secure=bool(c.get("secure")),
                             # r3 反馈：expires=0 是"已到期"值，不得 or None 变会话 Cookie
                             expires=(None if c.get("expires") is None
                                      else int(c.get("expires"))))
        except Exception:
            continue
    try:
        # 1) flow 登录链（Login.jsp 为 CAS service——登录态 cookie: loginidweaver）
        resp, err = _get_follow(sess, FLOW_LOGIN)
        if err:
            return {"outcome": "upstream_error", "items": None, "diag": err}
        if resp is None or resp.status_code != 200:
            return {"outcome": "http_error", "items": None,
                    "diag": "flow 登录页 HTTP %s" % (resp.status_code if resp else "无响应")}
        names = {c.name for c in sess.cookies if c.domain.endswith("flow.zjgsu.edu.cn")}
        if "loginidweaver" not in names:
            # CAS TGT 失效或风控——未建立登录态（不区分原因，不猜）
            return {"outcome": "upstream_error", "items": None,
                    "diag": "flow 登录态未建立（CAS 未放行/TGT 失效）"}
        # 2) 列表 sessionkey（datas 字段）
        r2 = sess.get(FLOW_BASE + "/api/workflow/mobile/getListResult",
                      params={"ismobile": 1, "viewScope": view_scope, "menuid": 10},
                      timeout=20, allow_redirects=False)
        if r2.status_code in (301, 302, 303, 307, 308):
            return {"outcome": "upstream_error", "items": None,
                    "diag": "getListResult 返回重定向（%s）——不自动跟随" % r2.status_code}
        if r2.status_code != 200:
            return {"outcome": "http_error", "items": None,
                    "diag": "getListResult HTTP %s" % r2.status_code}
        try:
            d2 = r2.json()
        except Exception:
            return {"outcome": "parse_error", "items": None,
                    "diag": "getListResult 响应非 JSON"}
        if not isinstance(d2, dict):
            return {"outcome": "parse_error", "items": None,
                    "diag": "getListResult 根非对象"}
        sk = d2.get("datas")
        if not isinstance(sk, str) or not sk.strip():
            # E9 的 sessionkey 失效/被拒时也走此处——如实按上游错误分类
            return {"outcome": "upstream_error", "items": None,
                    "diag": "getListResult 未返回 sessionkey（%s）" % (
                        d2.get("msg") or "datas 缺失")}
        # 3) 数据行（E9 标准数据集接口；form POST）
        r3 = sess.post(FLOW_BASE + "/api/ec/dev/table/datas",
                       data={"dataKey": sk, "current": max(1, int(current)),
                             "pageSize": max(1, min(int(page_size), 100))},
                       headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                "Accept": "application/json, text/javascript, */*; q=0.01"},
                       timeout=20, allow_redirects=False)
        if r3.status_code in (301, 302, 303, 307, 308):
            return {"outcome": "upstream_error", "items": None,
                    "diag": "table/datas 返回重定向（%s）——不自动跟随" % r3.status_code}
        if r3.status_code != 200:
            return {"outcome": "http_error", "items": None,
                    "diag": "table/datas HTTP %s" % r3.status_code}
        try:
            d3 = r3.json()
        except Exception:
            return {"outcome": "parse_error", "items": None,
                    "diag": "table/datas 响应非 JSON"}
        if not isinstance(d3, dict):
            return {"outcome": "parse_error", "items": None, "diag": "table/datas 根非对象"}
        rows = d3.get("datas")
        if rows is None:
            return {"outcome": "parse_error", "items": None,
                    "diag": "table/datas 缺 datas"}
        if not isinstance(rows, list):
            return {"outcome": "parse_error", "items": None,
                    "diag": "datas 结构未知（%s）" % type(rows).__name__}
        # 白名单投影（整行仅取 4 字段；标量 str 归一；嵌套/未知不透传）
        # 主键 requestid 缺失的行丢弃并计数（不产出半成品）；其余字段缺失如实 None
        items = []
        bad_rows = 0
        for row in rows:
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            entry = {}
            for src, dst in _ROW_FIELDS:
                v = row.get(src)
                entry[dst] = None if (v is None or isinstance(v, (dict, list))) else str(v)
            if not entry.get("request_id"):
                bad_rows += 1
                continue
            items.append(entry)
        return {"outcome": ("ok_empty" if not items else "ok"), "items": items,
                "bad_rows": bad_rows, "diag": ""}
    except requests.RequestException as e:
        # B03 反馈 P1：异常文本可能含含 ticket 的完整 URL——只回类型名
        return {"outcome": "network_error", "items": None,
                "diag": "网络错误（%s）" % type(e).__name__}
