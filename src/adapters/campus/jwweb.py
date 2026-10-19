# -*- coding: utf-8 -*-
"""教务网页版（正方 jwglxt）读取适配器：考试 / 成绩 / 网页课表。

端点与参数已实测核实（validation/jwweb/取证记录.md，2026-10-13 真实数据落盘）：
- 登录：CAS（service=index_initMenu.html）→ jwxt JSESSIONID（cookie 会话）
- F02 考试：POST /jwglxt/kwgl/kscx_cxXsksxxIndex.html?doType=query&gnmkdm=N358105
            响应含 totalCount/totalPage（实测 12/2，R21：必须遍历分页）
- F03 成绩：POST /jwglxt/cjcx/cjcx_cxXsgrcj.html?doType=query&gnmkdm=N305005
            响应含 totalCount/totalPage（实测 19/1）
- F06 课表：POST /jwglxt/kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N2151
            顶层 kbList（主课块）+ sjkList（实践环节课块，独立字段结构——R23 仅登记）
调用规律（取证记录 §三）：先 GET Index 页激活 gnmkdm 会话状态，再 POST doType=query。

边界：
- 会话由本机凭据层写入（login_cli init 时用同一凭据顺带完成 CAS 登录，存
  jwglxt_cookies=完整属性列表）；密码不落盘；CAS SSO cookie 不跨域泛化（R26）。
- 只做查询类请求（doType=query / cx 前缀），无状态变更。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import activation as _activation

MY_BASE = "https://my.zjgsu.edu.cn"
CAS_BASE = "https://uia.zjgsu.edu.cn/cas"
JWXT_BASE = "http://jwxt.zjgsu.edu.cn"
JW_SERVICE = JWXT_BASE + "/jwglxt/xtgl/index_initMenu.html"
_PUBKEY_PATH = "/v2/getPubKey"
# 证书校验保持开启；进程内复用 SSLContext（仅 HTTPS 路径受益：CAS / my.zjgsu）
_SSL_CONTEXT = ssl.create_default_context()
# 已认证页面的判定特征（index_initMenu 登录态含会话用户键；未登录会重定向到 CAS）
_AUTHED_MARK = "sessionUserKey"

EXAM_PATH = "/jwglxt/kwgl/kscx_cxXsksxxIndex.html"      # ?doType=query&gnmkdm=N358105
GRADE_PATH = "/jwglxt/cjcx/cjcx_cxXsgrcj.html"           # ?doType=query&gnmkdm=N305005
TIMETABLE_PATH = "/jwglxt/kbcx/xskbcx_cxXsgrkb.html"     # ?gnmkdm=N2151
EXAM_GNM = "N358105"
GRADE_GNM = "N305005"
TIMETABLE_GNM = "N2151"

# Index 页（激活 gnmkdm 会话状态用；GET 即可）
EXAM_INDEX_PATH = "/jwglxt/kwgl/kscx_cxXsksxxIndex.html"
GRADE_INDEX_PATH = "/jwglxt/cjcx/cjcx_cxDgXscj.html"
TIMETABLE_INDEX_PATH = "/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html"

# 成绩明细（N305007，实测 2026-10-14：KcList 15 门 → XmcjList 平时(50%)/期末(50%)/总评）
GRADE_MX_GNM = "N305007"
GRADE_KC_PATH = "/jwglxt/cjcx/cjcx_cxXsKcList.html"      # 有明细的课程列表（xnm+xqm）
GRADE_XM_PATH = "/jwglxt/cjcx/cjcx_cxXsXmcjList.html"    # 单课程项目成绩（+kch_id/jxb_id）
GRADE_MX_INDEX_PATH = "/jwglxt/cjcx/cjcx_cxDgXsxmcj.html"

# 实习成绩（N544030，实测 2026-10-14：本人全部学期合法空——接口 200+JSON 结构正常）
SX_GNM = "N544030"
SX_PATH = "/jwglxt/xssxgl/sxcjcx_cxSxcjcxIndex.html"    # ?doType=query（xnm/xqm/zmc/jg_id）
SX_INDEX_PATH = "/jwglxt/xssxgl/sxcjcx_cxSxcjcxIndex.html"

# 学业情况（N105515，实测 2026-10-14：统计在服务端渲染 HTML #alertBox；课程标签页为前端切换，无独立数据接口）
XY_GNM = "N105515"
XY_PATH = "/jwglxt/xsxy/xsxyqk_cxXsxyqkIndex.html"
# 学业预警处理查询（N305516，实测 2026-10-14：服务端渲染页直接显示「暂无预警」/预警列表）
YJCL_GNM = "N305516"
YJCL_PATH = "/jwglxt/xyyjjk/xyyjclcx_cxXyyjclcxxsIndex.html"
# 学籍预警节点树（N105505，实测 2026-10-14：26 个学分要求节点+33 门课程，含开课学期/学分）
XJYJ_GNM = "N105505"
XJYJ_INDEX_PATH = "/jwglxt/xjyj/xjyj_cxXjyjIndex.html"
XJYJ_JDLB_PATH = "/jwglxt/xjyj/xjyj_cxXjyjjdlb.html"
# 本人基本资料（N100801，实测 2026-10-14：GET 页面服务端渲染 31 字段 label→form-control-static）
GRXX_GNM = "N100801"
GRXX_PATH = "/xsxxxggl/xsgrxxwh_cxXsgrxx.html"

# 培养方案文件（N153045，实测 2026-10-14：全校 1182 个文件，pyfaxx_id/bbh/适用年级/总学分/附件标志）
PYFAWJ_GNM = "N153045"
PYFAWJ_PATH = "/jwglxt/pyfagl/pyfawjck_cxPyfawjckIndex.html"   # POST 同 URL 查询
# 附件正文下载（GET 二进制）。2026-10-18 实测（真机+PC 双通道）：端点为 pyfaxxwh
# （信息维护模块）；2.146 写成 pyfaxxxh 致 404 被误诊为「会话实时轮换受阻」，已证伪。
# id 必须用本会话列表返回 pyfaxx_id 的 32 位 hex（动态生成，跨会话失效）。
PYFAXXWH_PATH = "/jwglxt/pyfagl/pyfaxxwh_cxPyfaxxFj.html"
# 教学执行计划（N153540，实测 2026-10-14：njdm_id=2025 → 130 条，jxzxjhxx_id/zymc/总学分/课程数）
JXZXJH_GNM = "N153540"
JXZXJH_PATH = "/jwglxt/jxzxjhgl/jxzxjhck_cxJxzxjhckIndex.html"  # POST ?doType=query
JXZXJHKC_PATH = "/jwglxt/jxzxjhgl/jxzxjhkcxx_cxJxzxjhkcxxIndex.html"  # 单计划课程明细
# 重修课程查询（N255720，实测 2026-10-14：2026-2027-1 全校 1875 门重修课程）
CXKCCX_GNM = "N255720"
CXKCCX_PATH = "/jwglxt/cxkccx/cxkccx_cxCxkccxIndex.html"  # POST ?doType=query
# 网上上课地址（N1598，JS 取证 2026-10-14：paramMap=xnm/xqm+筛选；返回含 wsskdz 地址原文）
WSKCDZ_GNM = "N1598"
WSKCDZ_PATH = "/jwglxt/rwlscx/wsskdzwh_cxWsskdzwhIndex.html"  # POST ?doType=query（同上前缀规律）
# 授课提纲（N155002，实测 2026-10-14：本人课 2 条，含提纲 jl_id 供正文查询）
SKDG_GNM = "N155002"
SKDG_PATH = "/jwglxt/jsjxrl/jsjxrl_cxJsjxrlCx.html"  # POST ?doType=query（menu_list 无 /jwglxt 前缀，实际路由带）
# 创新创业获得分值（N4780，JS 取证：doType=query 返回本人分值记录；待在线验证）
SZF_GNM = "N4780"
SZF_PATH = "/jwglxt/xmfzgl/xshdfzcx_cxXshdfzcxIndex.html"
# 查询空闲教室（N2155，控件取证 2026-10-15：dm 学期（如 2026-3）/xqh_id 校区/lh 楼号/
# cdlb_id 类别/qszws-jszws 座位/cdmc 名称/qssj-jssj 日期范围）
KXJS_GNM = "N2155"
KXJS_PATH = "/jwglxt/cdjy/cdjy_cxKxcdlb.html"  # POST ?doType=query
# 班级课表（B01-07，实测 2026-10-15：kbcx 模块 xskbcx_cxXsKb 接受 bh_id 查班级课表）
BJKB_GNM = "N2151"
BJKB_PATH = "/jwglxt/kbcx/xskbcx_cxXsKb.html"  # POST ?doType=query（bh_id 必填）
JXZXJHKC_INDEX_PATH = "/jwglxt/jxzxjhgl/jxzxjhkcxx_cxJxzxjhkcxxIndex.html"

_PAGE_SIZE = 100   # 分页每页条数（页面默认 10/15，取大值减少请求次数；来源实测可接受）

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


# ---------------- 传输（自带 cookie 会话，与解析分离） ----------------

class JwwebTransport:
    """CAS/jwglxt 会话传输：GET 与 POST form，cookie 自动保存（按 domain/path/secure 匹配）。

    返回值约定与 UrllibTransport 一致：
    {'kind':'response','status':int,'text':str} / {'kind':'network_error','err':str}
    cookie 导出/恢复保留原始属性（R26）：CAS SSO cookie（iPlanetDirectoryPro，uia 域）
    只在 uia 域发送；教务会话 cookie（JSESSIONID/route，jwxt 域）只在 jwxt 域发送。
    """

    def __init__(self) -> None:
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPSHandler(context=_SSL_CONTEXT))

    def _request(self, url: str, data: bytes | None, headers: dict, timeout: int) -> dict:
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", UA)
        req.add_header("X-Requested-With", "XMLHttpRequest")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return {"kind": "response", "status": resp.status,
                        "text": resp.read().decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            return {"kind": "response", "status": e.code, "text": body}
        except Exception as e:
            return {"kind": "network_error", "err": str(e)}

    def get(self, url: str, headers: dict | None = None, timeout: int = 20) -> dict:
        return self._request(url, None, headers, timeout)

    def get_binary(self, url: str, headers: dict | None = None,
                   timeout: int = 30) -> dict:
        """GET 二进制响应（文件下载，如培养方案附件 docx）。

        返回 {'kind':'response','status':int,'body':bytes} /
        {'kind':'network_error','err':str}；不注入 X-Requested-With
        （文件下载为文档导航型请求，与 fetch 的 XHR 行为不同）。"""
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return {"kind": "response", "status": resp.status,
                        "body": resp.read()}
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            return {"kind": "response", "status": e.code, "body": body}
        except Exception as e:
            return {"kind": "network_error", "err": str(e)}

    def post_form(self, url: str, form: dict | None = None,
                  headers: dict | None = None, timeout: int = 20) -> dict:
        data = urllib.parse.urlencode(form or {}).encode("utf-8")
        return self._request(url, data, {"Content-Type": "application/x-www-form-urlencoded",
                                         **(headers or {})}, timeout)

    def export_cookies(self) -> list[dict]:
        """导出完整 cookie 属性（R26：域/path/secure 不丢失，恢复时不跨域）。"""
        out = []
        for c in self._jar:
            out.append({
                "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "secure": bool(c.secure),
                "expires": c.expires, "discard": bool(c.discard),
            })
        return out

    def session_fingerprint(self) -> str:
        """当前 cookie 会话的稳定指纹（激活账本键的一部分；轮换后自然失配）。"""
        return _activation.session_fingerprint(self.export_cookies())

    def set_cookies(self, cookies) -> None:
        """恢复会话（R26 剩余：严格格式校验，拒绝不合规格式而非自动迁移）。

        只接受 export_cookies 的完整属性列表：非空列表、每项含 name/value/domain
        （domain 缺失或空 → ValueError，调用方提示重新登录；旧 dict/缺域列表
        一律拒绝——CAS SSO cookie 不得被迁移到教务域泛化发送）。
        """
        import time as _t
        if not isinstance(cookies, list) or not cookies:
            raise ValueError("会话 Cookie 格式过旧（非完整属性列表）：请重新运行 login_cli init")
        for c in cookies:
            if (not isinstance(c, dict) or not c.get("name")
                    or not c.get("domain") or "value" not in c):
                raise ValueError("会话 Cookie 缺少 domain/name/value（旧格式）："
                                 "请重新运行 login_cli init 生成完整格式会话")
        for c in cookies:
            self._jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"],
                port=None, port_specified=False,
                domain=c["domain"], domain_specified=True,
                domain_initial_dot=str(c["domain"]).startswith("."),
                path=c.get("path") or "/", path_specified=True,
                secure=bool(c.get("secure")),
                expires=c.get("expires") or (int(_t.time()) + 86400),
                discard=bool(c.get("discard")),
                comment=None, comment_url=None, rest={}, rfc2109=False))


# ---------------- CAS 密码加密（与探针一致，纯 Python 复现） ----------------

def encrypt_password(pwd: str, modulus_hex: str, exponent_hex: str) -> str:
    """CAS 登录页加密：密码整串倒序 → RSA 分块（小端组整数 → pow(c,e,n) → hex）。

    依据：CAS login.js:208-210（倒序）与 security.js:634-662（分块小端）；
    实测 512 位 modulus → chunk=62（validation/service-center/取证记录.md）。
    """
    if any(ord(c) > 255 or ord(c) == 0 for c in pwd):
        raise ValueError("密码含非 ASCII 字符，与本加密方案不兼容")
    n = int(modulus_hex, 16)
    e = int(exponent_hex, 16)
    n_digits_16bit = (n.bit_length() + 15) // 16
    chunk = 2 * (n_digits_16bit - 1)
    if chunk <= 0:
        chunk = 2
    a = [ord(c) for c in pwd[::-1]]
    while len(a) % chunk != 0:
        a.append(0)
    out = []
    for i in range(0, len(a), chunk):
        block = int.from_bytes(bytes(a[i:i + chunk]), "little")
        out.append(format(pow(block, e, n), "x"))
    return " ".join(out)


def cas_login(username: str, password: str) -> dict:
    """CAS 登录并建立 jwglxt 会话。返回 {'outcome':'ok','cookies':[...],'diag':str}。

    R26：成功判据不是「某个 cookie 名存在」，而是
    (a) CAS 会话 cookie（iPlanetDirectoryPro）存在，且
    (b) 用该会话请求教务已认证页面（index_initMenu）返回 200 且含登录态特征
        （sessionUserKey）；凭 CAS cookie 名存在不构成教务会话证据。
    失败 outcome：network_error / login_failed / unexpected / not_authenticated。
    不抛异常、不写日志（凭据不进任何输出）。
    """
    t = JwwebTransport()
    r = t.get(CAS_BASE + "/login?service=" + urllib.parse.quote(JW_SERVICE, safe=""))
    if r["kind"] != "response" or r["status"] != 200:
        return {"outcome": "network_error", "cookies": [], "diag": "CAS 登录页获取失败(%s)" % r.get("status", r.get("err", "?"))}
    m = re.search(r'name="execution" value="([^"]+)"', r["text"])
    if not m:
        return {"outcome": "unexpected", "cookies": [], "diag": "CAS 登录页缺少 execution 字段"}
    execution = m.group(1)

    pk = t.get(CAS_BASE + _PUBKEY_PATH)
    if pk["kind"] != "response" or pk["status"] != 200:
        return {"outcome": "network_error", "cookies": [], "diag": "公钥获取失败"}
    try:
        import json as _json
        pkey = _json.loads(pk["text"])
        enc = encrypt_password(password, pkey["modulus"], pkey["exponent"])
    except Exception as e:
        return {"outcome": "unexpected", "cookies": [], "diag": "加密失败:%s" % type(e).__name__}

    r = t.post_form(CAS_BASE + "/login?service=" + urllib.parse.quote(JW_SERVICE, safe=""),
                    {"username": username, "password": enc,
                     "execution": execution, "_eventId": "submit"})
    ck = {c.name for c in t._jar}
    if "iPlanetDirectoryPro" not in ck:
        return {"outcome": "login_failed", "cookies": [],
                "diag": "CAS 未发放会话（凭据或风控原因）"}

    # 用 CAS 会话换 jwxt 会话（forceAuth 保证新票据），并核实已认证页面（R26）
    t.get(CAS_BASE + "/login?service=" + urllib.parse.quote(JW_SERVICE, safe="") + "&forceAuth=1")
    ck = {c.name for c in t._jar}
    if "JSESSIONID" not in ck:
        return {"outcome": "unexpected", "cookies": t.export_cookies(),
                "diag": "未取得 jwxt JSESSIONID"}
    verify = t.get(JW_SERVICE)
    if verify["kind"] != "response" or verify["status"] != 200 or _AUTHED_MARK not in verify["text"]:
        return {"outcome": "not_authenticated", "cookies": t.export_cookies(),
                "diag": "教务已认证页面校验未通过（末次请求状态=%s）"
                        % verify.get("status", verify.get("err", "?"))}
    # R59：App 域（my.zjgsu.edu.cn）**完整 ticket 交换**——CAS TGT → service ticket
    # → my.zjgsu 校验种 session → checkLogin 验证。失败不静默丢旧链（调用方保留）。
    app_cookies = []
    app_diag = ""
    try:
        import requests as _rq
        from http.cookiejar import CookieJar as _CJ
        # 独立会话：复制 t 的 CAS cookie（含 CASTGC/iPlanetDirectoryPro）
        sess_app = _rq.Session()
        sess_app.headers.update({"User-Agent": "Mozilla/5.0"})
        for c in (t.export_cookies() or []):
            try:
                sess_app.cookies.set(c.get("name"), c.get("value"),
                                     domain=(c.get("domain") or ".zjgsu.edu.cn"),
                                     path=c.get("path") or "/",
                                     secure=bool(c.get("secure")),
                                     expires=c.get("expires"))
            except Exception:
                pass
        # 1) CAS service=my.zjgsu（TGT 在——302 带 service ticket → my 域校验）
        r1 = sess_app.get(
            CAS_BASE + "/login?service=" +
            urllib.parse.quote("https://my.zjgsu.edu.cn/", safe=""),
            timeout=20, allow_redirects=True)
        # 2) 门户首页（完整会话——业务 API 需门户上下文）
        sess_app.get(MY_BASE + "/", timeout=20)
        # 3) checkLogin 验证（-9/0=已登录）
        r2 = sess_app.get(MY_BASE + "/api/checkLogin/info", timeout=15)
        try:
            info = r2.json()
        except Exception:
            info = {}
        logged = info.get("error_code") in (0, -9)
        if logged:
            # 3) 完整导出会话 cookie（保留全部属性；仅 zjgsu 域）
            for c in sess_app.cookies:
                dm = c.domain or ""
                if "zjgsu.edu.cn" in dm:
                    app_cookies.append({
                        "name": c.name, "value": c.value,
                        "domain": dm, "path": c.path or "/",
                        "secure": bool(c.secure),
                        "expires": c.expires, "discard": bool(c.discard),
                    })
            app_diag = "App 会话建立 ✓（checkLogin=%s，%d cookies）" % (
                info.get("error_code"), len(app_cookies))
        else:
            app_diag = "App 会话未建立（checkLogin=%s）" % info.get("error_code")
    except Exception as e:
        app_diag = "App 域异常（%s）" % type(e).__name__
    # R59：App 失败**不阻断**教务链；调用方（login_cli）保留旧 app_cookies

    return {"outcome": "ok", "cookies": t.export_cookies(),
            "app_cookies": app_cookies, "app_diag": app_diag,
            "diag": "CAS+jwglxt 会话建立并核验"}


# ---------------- 响应分类（R23：细分认证失效/解析故障/服务故障） ----------------

@dataclass
class JwwebFetch:
    outcome: str                # ok / partial_source / ok_empty / session_expired / parse_error / upstream_error / http_error / network_error
    items: list[dict] | None
    diag: str
    raw_count: int | None
    total_expected: int | None = None    # 来源声明的总数（分页对账用；课表无此元数据为 None）
    extras: dict = field(default_factory=dict)  # 伴随数据（如课表 sjkList；仅登记，不套主 schema）


_LOGIN_MARKS = ("统一身份认证", "cas/login", "请登录")


def classify_jwweb(r: dict, list_keys: tuple = ("items",)) -> tuple[str, str, dict | None]:
    """响应分类（R23）：返回 (outcome, diag, parsed)。

    - ok / ok_empty：JSON 对象且 list_keys 中任一键为列表
    - session_expired：明确指向登录的响应（HTML 含统一身份认证特征）
    - parse_error：200 但响应无法按 JSON 解析（服务故障或协议变化——不是未登录）
    - upstream_error：HTML/非 JSON 的其他服务错误页、正方业务异常（如 910）
    - http_error / network_error：传输层失败
    """
    if r.get("kind") == "network_error":
        return "network_error", "网络错误（本机诊断:%s）" % r.get("err", "?"), None
    status = r.get("status", 0)
    text = (r.get("text") or "").strip()
    if status != 200:
        return "http_error", "HTTP %s" % status, None
    if text.startswith("<"):
        head = text[:2000]
        if any(mark in head or mark.lower() in head.lower() for mark in _LOGIN_MARKS):
            return "session_expired", "HTML 页指向统一身份认证登录（会话失效）", None
        return "upstream_error", "HTML 错误页（非登录特征；服务故障或路径变化）", None
    import json as _json
    try:
        data = _json.loads(text)
    except Exception:
        return "parse_error", "响应非 JSON（200；解析失败，协议可能变化）", None
    if not isinstance(data, dict):
        return "parse_error", "JSON 顶层非对象", None
    # R64：业务 code 校验**前置**（code=500+items空+total0 不再误判合法空）
    code = data.get("code")
    if code is not None and code not in (200, "200"):
        # 正方业务异常（如 910：功能码未激活/参数缺失）→ 服务故障，不是会话失效
        return "upstream_error", "业务异常响应(code=%s, keys=%s)" % (
            code, list(data.keys())[:6]), None
    items = None
    for k in list_keys:
        if isinstance(data.get(k), list):
            items = data[k]
            break
    if items is None:
        return "upstream_error", "业务异常响应(code=%s, keys=%s)" % (
            code, list(data.keys())[:6]), None
    outcome = "ok_empty" if not items else "ok"
    return outcome, "", data


def raw_business_code(text: str) -> int | None:
    """只从 JSON 顶层取整数业务码；非 JSON/非对象/字符串码一律 None。

    专供激活逻辑窄判定（910=功能码未激活/参数缺失）。不得用它扩展
    「所有异常都重试」，也不改变 classify_jwweb 的既有语义。
    """
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _activation_key(transport, gnm: str):
    """账本键；开关关闭、无指纹或传输不支持时返回 None（保持旧行为）。"""
    if not _activation.memo_enabled():
        return None
    fp = _session_fp(transport)
    if not fp:
        return None
    return _activation.ActivationKey(host="jwxt.zjgsu.edu.cn",
                                     gnmkdm=gnm, session_fp=fp)


def _session_fp(transport) -> str:
    try:
        return transport.session_fingerprint() or ""
    except Exception:
        return ""


def _activation_get_error(r: dict) -> tuple[str, str] | None:
    """激活 GET 的失败判定：传输失败/HTTP 非 200/登录页 → 具名错误。

    正常 Index 页是 HTML 且非登录特征 → 视为成功。**发起了 GET 不等于
    激活成功**：失败时调用方必须终止本次「空结果核验/910 重试」，
    保留既有成功快照（服务层 partial/旧快照契约处理）。
    """
    if r.get("kind") == "network_error":
        return ("network_error", "激活请求失败（网络错误，本机诊断:%s）"
                % r.get("err", "?"))
    status = r.get("status", 0)
    text = (r.get("text") or "").strip()
    if status != 200:
        return ("http_error", "激活请求失败：HTTP %s" % status)
    if text.startswith("<"):
        head = text[:2000]
        if any(mark in head or mark.lower() in head.lower()
               for mark in _LOGIN_MARKS):
            return ("session_expired", "激活页指向统一身份认证登录（会话失效）")
    return None


def _common_headers() -> dict:
    return {"Referer": JW_SERVICE}


def _fetch_paged(transport: JwwebTransport, path: str, gnm: str, index_path: str,
                 base_form: dict, list_key: str, activate_index: bool = True) -> JwwebFetch:
    key = _activation_key(transport, gnm) if activate_index else None
    if key is None:
        return _fetch_paged_impl(transport, path, gnm, index_path, base_form,
                                 list_key, activate_index, key=None)
    # 会话级协调：同会话（含不同功能码）的激活与查询整次串行，
    # 不做“按功能码各拿一把锁”的部分协调。
    with _activation.LEDGER.session_guard(key.session_fp):
        return _fetch_paged_impl(transport, path, gnm, index_path, base_form,
                                 list_key, activate_index, key=key)


def _fetch_paged_impl(transport: JwwebTransport, path: str, gnm: str, index_path: str,
                      base_form: dict, list_key: str, activate_index: bool,
                      key=None) -> JwwebFetch:
    """分页遍历（R21）：按来源 totalCount/totalPage 遍历、去重、对账。

    - 全部页取到且条数对账一致 → ok
    - 来源声明 total>0 但有页取不到/条数不齐 → partial_source（items 为已取到的，
      total_expected/diag 记录缺口；调用方按 partial 呈现，不得称全量）
    - total==0 且 items 空 → ok_empty（空结果证据 = 零总数元数据）
    """
    skipped = False
    if activate_index:
        # gnmkdm 会话状态激活（实测一次即可；同功能码的后续 POST 无需重复激活）
        if key is not None and _activation.LEDGER.is_active(key):
            skipped = True   # 仅在开关开启且同会话已确认激活时跳过
        else:
            get_r = transport.get(JWXT_BASE + index_path + "?gnmkdm=" + gnm,
                                  headers=_common_headers())
            gate = _activation_get_error(get_r)
            if gate is not None:
                if key is not None:
                    _activation.LEDGER.invalidate(key)
                return JwwebFetch(gate[0], None, gate[1], None)
    items: list[dict] = []
    seen: set[str] = set()
    total_expected = None
    page = 1
    max_pages = 20  # 防御上限（来源异常时不无限请求）

    def _to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    while page <= max_pages:
        form = dict(base_form)
        form["queryModel.showCount"] = str(_PAGE_SIZE)
        form["queryModel.currentPage"] = str(page)
        form["queryModel.pageable"] = "true"
        def _post_once():
            return transport.post_form(
                JWXT_BASE + path + "?doType=query&gnmkdm=" + gnm,
                dict(form), headers=_common_headers())

        r = _post_once()
        code = (raw_business_code(r.get("text"))
                if key is not None and r.get("status") == 200 else None)
        outcome, diag, data = classify_jwweb(r, list_keys=(list_key,))
        empty_reverified = False
        if page == 1 and key is not None:
            # 910（业务层未激活；仅 200 响应才可能是 910）或「跳过激活后拿到
            # 空结果」→ 补激活并只重试一次。**补激活 GET 失败即终止**：
            # 不得把未核验的第二次 POST 当作空结果证据。
            if code == 910 or (skipped and outcome == "ok_empty"):
                _activation.LEDGER.invalidate(key)
                get_r = transport.get(JWXT_BASE + index_path + "?gnmkdm=" + gnm,
                                      headers=_common_headers())
                gate = _activation_get_error(get_r)
                if gate is not None:
                    return JwwebFetch(gate[0], None, gate[1], None)
                r = _post_once()
                code = (raw_business_code(r.get("text"))
                        if r.get("status") == 200 else None)
                outcome, diag, data = classify_jwweb(r, list_keys=(list_key,))
                empty_reverified = (outcome == "ok_empty")
            if outcome in ("ok", "ok_empty") and code != 910:
                _activation.LEDGER.mark(key)
                if empty_reverified:
                    diag = ((diag + "；" if diag else "")
                            + "已重新激活核验，来源仍为空")
            else:
                _activation.LEDGER.invalidate(key)
        if outcome not in ("ok", "ok_empty"):
            if items:
                return JwwebFetch("partial_source", items,
                                  "第%d页起读取失败（%s）；已取得%d条，来源声明总数%s"
                                  % (page, diag, len(items), total_expected), len(items),
                                  total_expected=total_expected)
            return JwwebFetch(outcome, None, diag, None)
        if outcome == "ok_empty" and page == 1:
            tc = _to_int(data.get("totalCount"))
            tr = _to_int(data.get("totalResult"))
            if tc == 0 and tr in (0, None):
                note = "来源零总数（空结果证据）"
                if empty_reverified:
                    note += "；已重新激活核验，来源仍为空"
                return JwwebFetch("ok_empty", [], note, 0, total_expected=0)
            if tc in (None, "") and tr in (None, ""):
                note = "来源未返回总数元数据（该次该范围为空）"
                if empty_reverified:
                    note += "；已重新激活核验，来源仍为空"
                return JwwebFetch("ok_empty", [], note, 0, total_expected=None)
            # R21 剩余：totalCount=0 但 totalResult>0 等矛盾 → 缺口，不包装成合法空
            return JwwebFetch("partial_source", [],
                              "首页空但来源声明总数不一致（totalCount=%s, totalResult=%s）"
                              % (tc, tr), 0, total_expected=tr if tr is not None else tc)
        chunk = data[list_key]
        added = 0
        import hashlib as _hashlib
        for it in chunk:
            # R21 剩余：非对象条目不假设可取键；仅完全相同记录去重（内容全哈希），
            # 同课程不同成绩等不同记录一律保留
            if not isinstance(it, dict):
                # 首页坏条目 → 具名错误；后续页坏条目 → 保留已读首屏为 partial
                if page == 1 and not items:
                    return JwwebFetch("parse_error", None,
                                      "第%d页存在非对象条目（协议异常）" % page, None)
                return JwwebFetch("partial_source", items,
                                  "第%d页存在非对象条目（协议异常）；已保留前序%d条"
                                  % (page, len(items)), len(items),
                                  total_expected=total_expected)
            digest = _hashlib.sha1(
                json.dumps(it, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if digest not in seen:
                seen.add(digest)
                items.append(it)
                added += 1
        # total 元数据容错：字符串页数/总数均可，互相对账（R21 剩余）
        # §2.27①：tc/tr 矛盾检查独立于首次赋值（首页有数据且两总数不一致也必须拦截）
        tc = _to_int(data.get("totalCount"))
        tr = _to_int(data.get("totalResult"))
        if tc is not None and tr is not None and tc != tr:
            return JwwebFetch("partial_source", items,
                              "第%d页 totalCount(%s) 与 totalResult(%s) 不一致"
                              % (page, tc, tr), len(items), total_expected=total_expected)
        if total_expected is None:
            total_expected = tc if tc is not None else tr
        if page > 1 and total_expected is not None and tc is not None and tc != total_expected:
            # R21 剩余：跨页总数变化 → 对账失败，保留已读部分
            return JwwebFetch("partial_source", items,
                              "第%d页总数(%s)与首页(%s)不一致" % (page, tc, total_expected),
                              len(items), total_expected=total_expected)
        total_page = _to_int(data.get("totalPage"))
        if not total_page or total_page <= 0:
            total_page = page  # 来源未给页数：保守结束
        if page >= total_page:
            break
        page += 1
        if added == 0:
            break  # 整页完全重复：结束条件已达成
    # 对账（R21）
    if isinstance(total_expected, int) and len(items) != total_expected:
        return JwwebFetch("partial_source", items,
                          "遍历完成但对账不符：取得%d条，来源声明%d条"
                          % (len(items), total_expected), len(items),
                          total_expected=total_expected)
    return JwwebFetch("ok", items, "", len(items), total_expected=total_expected)


def fetch_exams(transport: JwwebTransport, xnm: str, xqm: str) -> JwwebFetch:
    """F02 考试信息（学生视角最小参数；分页遍历）。"""
    return _fetch_paged(
        transport, EXAM_PATH, EXAM_GNM, EXAM_INDEX_PATH,
        {"xnm": xnm, "xqm": xqm, "ksmcdmb_id": "", "kch": "", "kc": "",
         "ksrq": "", "kkbm_id": ""}, "items")


def fetch_grades(transport: JwwebTransport, xnm: str, xqm: str) -> JwwebFetch:
    """F03 学生成绩总表（学生视角；分页遍历）。"""
    return _fetch_paged(
        transport, GRADE_PATH, GRADE_GNM, GRADE_INDEX_PATH,
        {"xnm": xnm, "xqm": xqm, "sy_id": "", "sq_id": "", "kccjpmfs": "",
         "kcbj": "", "pkey": "", "_search": "false", "sortName": "", "sortOrder": "asc"},
        "items")


def fetch_timetable_web(transport: JwwebTransport, xnm: str, xqm: str) -> JwwebFetch:
    """F06 网页版个人课表（含教师工号 jgh 与姓名 xm）。"""
    key = _activation_key(transport, TIMETABLE_GNM)
    if key is None:
        return _fetch_timetable_web_impl(transport, xnm, xqm, key=None)
    with _activation.LEDGER.session_guard(key.session_fp):
        return _fetch_timetable_web_impl(transport, xnm, xqm, key=key)


def _fetch_timetable_web_impl(transport: JwwebTransport, xnm: str, xqm: str,
                              key=None) -> JwwebFetch:
    """F06 实现。

    R23：kbList 为主课块；sjkList（实践环节课块，起止周形式，独立字段结构）
    仅登记条数与摘要到 extras/diag，不套用主 schema、不丢弃不猜测。
    """
    skipped = False
    if key is not None and _activation.LEDGER.is_active(key):
        skipped = True
    else:
        get_r = transport.get(JWXT_BASE + TIMETABLE_INDEX_PATH + "?gnmkdm=" + TIMETABLE_GNM,
                              headers=_common_headers())
        gate = _activation_get_error(get_r)
        if gate is not None:
            if key is not None:
                _activation.LEDGER.invalidate(key)
            return JwwebFetch(gate[0], None, gate[1], None)

    def _post_once():
        return transport.post_form(
            JWXT_BASE + TIMETABLE_PATH + "?gnmkdm=" + TIMETABLE_GNM,
            {"xnm": xnm, "xqm": xqm, "gnmkdm": TIMETABLE_GNM},
            headers=_common_headers())

    r = _post_once()
    code = (raw_business_code(r.get("text"))
            if key is not None and r.get("status") == 200 else None)
    outcome, diag, data = classify_jwweb(r, list_keys=("kbList", "items"))
    if key is not None and (code == 910 or (skipped and outcome == "ok_empty")):
        _activation.LEDGER.invalidate(key)
        get_r = transport.get(JWXT_BASE + TIMETABLE_INDEX_PATH + "?gnmkdm=" + TIMETABLE_GNM,
                              headers=_common_headers())
        gate = _activation_get_error(get_r)
        if gate is not None:
            return JwwebFetch(gate[0], None, gate[1], None)
        r = _post_once()
        code = (raw_business_code(r.get("text"))
                if r.get("status") == 200 else None)
        outcome, diag, data = classify_jwweb(r, list_keys=("kbList", "items"))
        if outcome == "ok_empty":
            diag = ((diag + "；" if diag else "")
                    + "已重新激活核验，来源仍为空")
    if key is not None:
        if outcome in ("ok", "ok_empty") and code != 910:
            _activation.LEDGER.mark(key)
        else:
            _activation.LEDGER.invalidate(key)
    if outcome in ("ok", "ok_empty"):
        items = data.get("kbList") or data.get("items") or []
        sjk = data.get("sjkList") or []
        extras = {}
        if sjk:
            # 登记语义：实践环节课块（起止周 qsjsz）；原样原文保留，不套主 schema
            extras["sjk_count"] = len(sjk)
            extras["sjk_summary"] = [
                {"course_name": s.get("kcmc"), "teacher": s.get("jsxm"),
                 "weeks_text": s.get("qsjsz")} for s in sjk if isinstance(s, dict)]
            diag = (diag + ("；" if diag else "") +
                    "另有实践环节课块 sjkList=%d 条（起止周形式，未并入本输出）" % len(sjk))
        return JwwebFetch(outcome, items, diag, len(items), total_expected=None,
                          extras=extras)
    return JwwebFetch(outcome, None, diag, None)


# ---------------- 条目映射（白名单字段，不猜口径） ----------------

_EXAM_FIELDS = {"ksmc": "exam_name", "kcmc": "course_name", "kch": "course_code",
                "kssj": "exam_datetime_text",    # 实测字段：如 "2026-07-01(08:40-10:40)"
                "sksj": "weekly_slots_text",     # 实测字段：如 "星期二第3-5节{1-16周}"（课程原排课时间）
                "cdmc": "room", "cdlbmc": "room_type",
                "ksfs": "exam_mode", "bj": "class_name", "kkxy": "school",
                "xqm": "semester_code", "sjbh": "paper_no"}

_GRADE_FIELDS = {"kcmc": "course_name", "kch": "course_code", "cj": "score",
                 "jd": "grade_point", "xf": "credit", "jsxm": "teachers_text",
                 "jxbmc": "teaching_class", "kcxzmc": "course_type",
                 "khfsmc": "assessment_mode", "kclbmc": "course_category",
                 "ksxz": "exam_nature"}   # 考试性质原文（如"补考一"；第三学期=补考/重修常规位置）

_KB_FIELDS = {"kcmc": "course_name", "kch": "course_code", "jxbmc": "teaching_class",
              "jc": "period_text", "jcs": "period_span", "xqj": "weekday",
              "xqjmc": "weekday_text", "zcd": "weeks_text", "cdmc": "room",
              "jgh": "teacher_id", "xm": "teacher_name", "xf": "credit",
              "kcxz": "course_type", "kclbmc": "course_category", "xkrs": "enrolled",
              "jxbzc": "class_list"}

# 注意（R22）：App 侧 jsbh→teacher_id 的语义未核实；本文件 jgh（网页工号）+xm（姓名）
# 是同记录实证语义，二者不得跨源互替。

_GRADE_COURSE_FIELDS = {"kcmc": "course_name", "kch_id": "course_code",
                        "jxbmc": "teaching_class", "zpcj": "total_score",
                        "kkbmmc": "school", "xf": "credit"}

_GRADE_COMPONENT_FIELDS = {"xmblmc": "component_name",   # 如 "平时(50%)"——原文含权重
                           "xmcj": "component_score"}

_INTERNSHIP_FIELDS = {"kcmc": "course_name", "kch": "course_code",
                      "zzcj": "final_score",             # 总评成绩（原文）
                      "jsxm": "teacher", "xmmc": "project_name",
                      "jdmc": "campus", "jxbmc": "teaching_class",
                      "zmc": "group_name"}

_SYLLABUS_FIELDS = {"kch_id": "course_code", "kcmc": "course_name",
                    "jsxm": "teacher", "jxbmc": "teaching_class",
                    "jxbzc": "teaching_group", "jxb_id": "teaching_class_id",
                    "jlid": "syllabus_id",            # 提纲记录 id（正文查询后续接入）
                    "kcxzmc": "course_type", "kklxmc": "open_kind",
                    "kkxy": "school", "bpkbj": "is_arranged",
                        "rlzt": "syllabus_status_code", "rlztmc": "syllabus_status"}


_ONLINE_URL_FIELDS = {"kch": "course_code", "kcmc": "course_name",
                      "jsxm": "teacher", "jgh": "teacher_id",
                      "jxbmc": "teaching_class", "jxdd": "location_text",
                      "sksj": "class_time_text", "wsskdz": "online_url",
                      "wsskfjlj": "attachment_path", "wsskfjm": "attachment_name",
                      "qqqh": "group_number", "xf": "credit", "xkrs": "enrolled",
                      "xiaoqmc": "campus", "xnmc": "academic_year",
                      "xqmc": "semester", "kcxzmc": "course_type",
                      "kclbmc": "course_category", "kcgsmc": "course_format"}

_RETAKE_FIELDS = {"kch": "course_code", "kcmc": "course_name",
                  "kclbmc": "course_category", "kkbmmc": "school", "xf": "credit",
                  "cxxnmc": "retake_year", "cxxqmc": "retake_term",
                  "cxzt": "retake_status"}

_TRAINING_PLAN_FIELDS = {"pyfamc": "plan_name",          # 如 "2023软件工程"
                         "pyfaxx_id": "plan_ref",        # 文件标识（附件下载后续接入）
                         "bbh": "version",
                         "njdm_id": "apply_grades",      # 适用年级列表原文（逗号分隔）
                         "jgmc": "school", "zymc": "major_name",
                         "zdxf": "total_credit",
                         "hasPyfaxxFj": "has_attachment", "dlbs": "plan_kind",
                         "zyh": "major_code"}

_TEACHING_PLAN_COURSE_FIELDS = {"kch": "course_code", "kcmc": "course_name",
                                "kcxzmc": "course_type", "kclbmc": "course_category",
                                "xf": "credit", "xqmc": "campus",
                                "xfyqjdmc": "credit_node_name",
                                "xsxxxx": "hours_text",       # 如 "理论(2.0)-实验(2.0)"
                                "jyxdxnm": "term_year", "jyxdxqm": "term_code",
                                "kkbmmc": "school"}

_TEACHING_PLAN_FIELDS = {"jxzxjhxx_id": "plan_ref", "njmc": "grade",
                         "zymc": "major_name", "zyh": "major_code",
                         "zdxf": "total_credit", "kcs": "course_count",
                         "xqmc": "campus", "jg_id": "school_code",
                         "jhrs": "planned_seats", "dlbs": "plan_kind",
                         "sfgazy": "is_higher_vocational"}


def fetch_grade_courses(transport: JwwebTransport, xnm: str, xqm: str) -> JwwebFetch:
    """F03 明细第一步：有明细的课程列表（xnm+xqm）。"""
    return _fetch_paged(transport, GRADE_KC_PATH, GRADE_MX_GNM, GRADE_MX_INDEX_PATH,
                        {"xnm": xnm, "xqm": xqm}, "items")


# 注意：成绩组成行没有 course_name 字段——映射时 require_course_name=False。


def fetch_my_profile(transport: JwwebTransport) -> dict:
    """F04 本人基本资料（N100801）：GET 服务端渲染页，label→form-control-static 解析。
    实测 2026-10-14 离线解析 31 字段（学号/姓名/性别/专业/班级/学籍状态等）。"""
    r = transport.get(JWXT_BASE + GRXX_PATH + "?gnmkdm=" + GRXX_GNM,
                      headers=_common_headers())
    if r.get("kind") == "network_error":
        return {"outcome": "network_error", "profile": None, "diag": "网络错误"}
    if r.get("status") != 200:
        return {"outcome": "http_error", "profile": None, "diag": "HTTP %s" % r.get("status")}
    text = r.get("text") or ""
    if "form-control-static" not in text:
        if any(mk in text[:2000] for mk in _LOGIN_MARKS):
            return {"outcome": "session_expired", "profile": None, "diag": "会话失效"}
        return {"outcome": "upstream_error", "profile": None, "diag": "无资料块"}
    pairs = re.findall(
        r'control-label[^>]*>\s*([^<：：]{2,12})：?\s*</label>\s*<div[^>]*>\s*'
        r'<p class="form-control-static">([^<]{0,60})</p>', text)
    if not pairs:
        return {"outcome": "parse_error", "profile": None, "diag": "资料块解析为空"}
    profile = {}
    for k, v in pairs:
        k, v = k.strip(), v.strip()
        if v and k not in profile:
            profile[k] = v
    return {"outcome": "ok", "profile": profile, "diag": ""}


def fetch_academic_warnings(transport: JwwebTransport) -> dict:
    """学业预警处理查询（N305516）：GET 服务端渲染页，提取本人预警状态正文。
    页面无预警时显示「…同学您好：当前学年学期暂无预警！」；有预警时渲染预警列表
    （列表结构未知——以 raw_text 原文呈现，不解析推导）。"""
    r = transport.get(JWXT_BASE + YJCL_PATH + "?gnmkdm=" + YJCL_GNM,
                      headers=_common_headers())
    if r.get("kind") == "network_error":
        return {"outcome": "network_error", "summary": None, "raw": None,
                "diag": "网络错误（%s）" % r.get("err", "?")}
    if r.get("status") != 200:
        return {"outcome": "http_error", "summary": None, "raw": None,
                "diag": "HTTP %s" % r.get("status")}
    text = r.get("text") or ""
    if text.strip().startswith("<") and "同学您好" not in text:
        if any(m in text[:2000] for m in _LOGIN_MARKS):
            return {"outcome": "session_expired", "summary": None, "raw": None,
                    "diag": "HTML 指向登录（会话失效）"}
        return {"outcome": "upstream_error", "summary": None, "raw": None,
                "diag": "HTML 无预警内容块"}
    import re as _re
    body = _re.sub(r"<script[\s\S]*?</script>", "", text)
    body = _re.sub(r"<style[\s\S]*?</style>", "", body)
    body = _re.sub(r"<[^>]+>", " ", body)
    body = _re.sub(r"(&nbsp;|\s)+", " ", body)
    m = _re.search(r"([一-龥]{2,4}同学您好：.{0,600})", body)
    if not m:
        return {"outcome": "parse_error", "summary": None, "raw": None,
                "diag": "未找到预警状态块（页面结构可能变化）"}
    raw = m.group(1).strip()
    no_warning = "暂无预警" in raw
    return {"outcome": "ok", "summary": raw[:400], "no_warning": no_warning,
            "raw": raw, "diag": ""}


def fetch_credit_requirements(transport: JwwebTransport) -> dict:
    """F04 学分要求节点树（N105505）：培养方案各学分要求节点+节点下课程
    （开课学期/学分/已得学分 yxxf）。学号从菜单页 sessionUserKey 自动获取
    （账号域内部流转，不进工具参数）。jg_id/njdm_id 从预警页面下拉默认值解析
    （学生视角自动定位本人学院/年级）。"""
    # 1) 学号
    r = transport.get(JWXT_BASE + "/jwglxt/xtgl/index_initMenu.html",
                      headers=_common_headers())
    if r.get("kind") == "network_error" or r.get("status") != 200:
        return {"outcome": "network_error", "nodes": None, "diag": "菜单页获取失败"}
    m = re.search(r'id="sessionUserKey"[^>]*value="([^"]*)"', r.get("text") or "")
    if not m:
        if any(mk in (r.get("text") or "")[:2000] for mk in _LOGIN_MARKS):
            return {"outcome": "session_expired", "nodes": None, "diag": "会话失效"}
        return {"outcome": "parse_error", "nodes": None, "diag": "菜单页无 sessionUserKey"}
    xh = m.group(1)
    import json as _json
    # 2) 预警页面（拿 jg_id/njdm_id 默认值 + 激活 gnmkdm）
    r = transport.get(JWXT_BASE + XJYJ_INDEX_PATH + "?gnmkdm=" + XJYJ_GNM,
                      headers=_common_headers())
    if r.get("kind") == "network_error" or r.get("status") != 200:
        return {"outcome": "network_error", "nodes": None, "diag": "预警页获取失败"}
    page = r.get("text") or ""
    if "统一身份认证" in page[:2000]:
        return {"outcome": "session_expired", "nodes": None, "diag": "会话失效"}
    jg = nj = None
    m2 = re.search(r'id="jg_id"[^>]*>(.{0,3000}?)</select>', page, re.S)
    if m2:
        sel = re.findall(r'<option[^>]*value="([^"]*)"[^>]*selected', m2.group(1))
        jg = sel[0] if sel else ""
    m3 = re.search(r'id="njdm_id"[^>]*>(.{0,3000}?)</select>', page, re.S)
    if m3:
        sel = re.findall(r'<option[^>]*value="([^"]*)"[^>]*selected', m3.group(1))
        nj = sel[0] if sel else ""
    # 3) zyh_id 自动获取：成绩总表找本人班级名 → 班级推荐课表列表匹配 zyh_id
    #    （R37 精神：身份参数在账号域内部流转，不进工具参数）
    bj_name = None
    for xnm_try, xqm_try in [("2026", "3"), ("2025", "16"), ("2025", "12"), ("2025", "3")]:
        r = transport.post_form(
            JWXT_BASE + "/jwglxt/cjcx/cjcx_cxXsgrcj.html?doType=query&gnmkdm=N305005",
            {"xnm": xnm_try, "xqm": xqm_try, "queryModel.showCount": "1",
             "queryModel.currentPage": "1"},
            headers=_common_headers())
        if r.get("kind") != "response" or r.get("status") != 200:
            continue
        try:
            d = _json.loads(r.get("text") or "{}")
            row = (d.get("items") or [{}])[0]
            if row.get("bj"):
                bj_name = row["bj"]
                break
        except Exception:
            continue
    zyh = ""
    if bj_name:
        r = transport.post_form(
            JWXT_BASE + "/jwglxt/kbdy/bjkbdy_cxBjkbdyTjkbList.html?gnmkdm=N214505",
            {"xnm": "2026", "xqm": "3", "njdm_id": nj or "", "jg_id": jg or "",
             "zyh_id": "", "bj_id": "", "bh_id": "",
             "queryModel.showCount": "200", "queryModel.currentPage": "1"},
            headers=_common_headers())
        try:
            d = _json.loads(r.get("text") or "{}")
            matches = [row.get("zyh_id") for row in (d.get("items") or [])
                       if row.get("bj") == bj_name and row.get("zyh_id")]
            if len(set(matches)) == 1:
                zyh = matches[0]
        except Exception:
            pass
    # 4) 节点树
    r = transport.post_form(
        JWXT_BASE + XJYJ_JDLB_PATH + "?gnmkdm=" + XJYJ_GNM + "&id=" + xh,
        {"jg_id": jg or "", "njdm_id": nj or "", "zyh_id": zyh},
        headers=_common_headers())
    if r.get("kind") == "network_error" or r.get("status") != 200:
        return {"outcome": "network_error", "nodes": None, "diag": "节点树获取失败"}
    import json as _json
    try:
        nodes = _json.loads(r.get("text") or "")
    except Exception:
        return {"outcome": "parse_error", "nodes": None, "diag": "节点树非 JSON"}
    if nodes is None:
        # R43：来源返回 null=协议错误/无数据不明——不是合法空列表
        return {"outcome": "parse_error", "nodes": None,
                "diag": "来源返回 null（非节点列表，协议异常）"}
    if not isinstance(nodes, list):
        return {"outcome": "parse_error", "nodes": None,
                "diag": "来源返回非列表结构（%s）" % type(nodes).__name__}
    return {"outcome": "ok", "nodes": nodes, "diag": ""}


def fetch_academic_progress(transport: JwwebTransport) -> dict:
    """F04 学业情况（N105515）：GET 服务端渲染页，解析 #alertBox 统计。

    返回 {'outcome', 'stats'|None, 'diag'}。统计含 GPA/计划门数/通过/未通过/
    未修/在读/计划外通过/计划外未通过/统计时间——全部为来源原文，不做推导。
    课程级列表（已修/在修/未修标签）为前端渲染、无独立数据接口，本轮不解析
    （缺口由调用方登记）。
    """
    r = transport.get(JWXT_BASE + XY_PATH + "?gnmkdm=" + XY_GNM,
                      headers=_common_headers())
    if r.get("kind") == "network_error":
        return {"outcome": "network_error", "stats": None,
                "diag": "网络错误（%s）" % r.get("err", "?")}
    if r.get("status") != 200:
        return {"outcome": "http_error", "stats": None,
                "diag": "HTTP %s" % r.get("status")}
    text = r.get("text") or ""
    if text.strip().startswith("<") and "alertBox" not in text:
        if any(m in text[:2000] for m in _LOGIN_MARKS):
            return {"outcome": "session_expired", "stats": None,
                    "diag": "HTML 指向登录（会话失效）"}
        return {"outcome": "upstream_error", "stats": None, "diag": "HTML 无学业统计块"}
    import re as _re

    def _strip(seg):
        seg = _re.sub(r"<!--.*?-->", "", seg, flags=_re.S)
        seg = _re.sub(r"<[^>]+>", " ", seg)
        return _re.sub(r"(&nbsp;|\s)+", " ", seg)

    m = _re.search(r'id="alertBox"(.{0,4000}?)提示', text, _re.S)
    if not m:
        return {"outcome": "parse_error", "stats": None,
                "diag": "未找到学业统计块（页面结构可能变化）"}
    plain = _strip(m.group(1))
    stats = {"raw_text": plain.strip()[:400]}

    def _num(pat):
        mm = _re.search(pat, plain)
        return mm.group(1) if mm else None

    stats["gpa"] = _num(r"（GPA）：\s*([\d.]+)")
    stats["planned_courses"] = _num(r"计划总课程\s*(\d+)\s*门")
    mm = _re.search(r"通过\s*(\d+)\s*门，?\s*未通过\s*(\d+)\s*门", plain)
    stats["passed"] = mm.group(1) if mm else None
    stats["failed"] = mm.group(2) if mm else None
    stats["not_started"] = _num(r"未修\s*(\d+)\s*门")
    stats["in_progress"] = _num(r"在读\s*(\d+)\s*门")
    mm2 = _re.search(r"计划外：\s*通过\s*(\d+)\s*门，?\s*未通过\s*(\d+)\s*门", plain)
    stats["extra_passed"] = mm2.group(1) if mm2 else None
    stats["extra_failed"] = mm2.group(2) if mm2 else None
    mm3 = _re.search(r"统计时间\s*([\d\-: ]+?)之前有效", plain)
    stats["stat_time_text"] = mm3.group(1).strip() if mm3 else None
    if not stats["gpa"]:
        return {"outcome": "parse_error", "stats": None,
                "diag": "统计块存在但 GPA 解析失败（结构变化）"}
    return {"outcome": "ok", "stats": stats, "diag": ""}


def fetch_training_plans(transport: JwwebTransport, year: str) -> JwwebFetch:
    """F04 培养方案文件（N153045）：synj=适用年级过滤（如 2025）。
    返回全校适用该年级的培养方案文件（名称/版本/适用年级列表/总学分/附件标志）。"""
    return _fetch_paged(transport, PYFAWJ_PATH, PYFAWJ_GNM, PYFAWJ_PATH,
                        {"jg_id": "", "synj": year, "dlbs": "", "zyh_id": ""}, "items")


def fetch_training_plan_doc(transport: JwwebTransport, pyfaxx_id: str) -> dict:
    """F04 培养方案附件正文下载（N153045：pyfaxxwh_cxPyfaxxFj.html，GET 二进制）。

    激活 GET 纳入会话级协调，并做传输/HTTP/登录页门禁（失败不进入下载）。
    """
    fp = _session_fp(transport)
    if not fp:
        return _fetch_training_plan_doc_impl(transport, pyfaxx_id)
    with _activation.LEDGER.session_guard(fp):
        return _fetch_training_plan_doc_impl(transport, pyfaxx_id)


def _fetch_training_plan_doc_impl(transport: JwwebTransport, pyfaxx_id: str) -> dict:
    """F04 附件下载实现。

    2026-10-18 实测（真机 + PC cas_login 双通道验证）：
    - 下载前必须 GET Index 激活 N153045 会话菜单状态，否则返回 zftal「出错啦」HTML；
    - id 为本会话列表返回 pyfaxx_id 的 32 位 hex（动态生成，跨会话失效——
      用过期 id 会得到「出错啦」页而非 404/登录页）；
    - 成功响应 application/msword，PK 头 zip（docx）。

    返回：{'outcome':'ok','content_type':str,'size':int,'body':bytes}
      / {'outcome':'session_expired'|'upstream_error'|'http_error'|'network_error',
         'diag':str}
    """
    # ① 菜单状态激活（与 _fetch_paged 同款；一次即可；失败不进入下载）
    get_r = transport.get(JWXT_BASE + PYFAWJ_PATH + "?gnmkdm=" + PYFAWJ_GNM,
                          headers=_common_headers())
    gate = _activation_get_error(get_r)
    if gate is not None:
        return {"outcome": gate[0], "diag": gate[1]}
    # ② 下载（文档导航型 GET；引用页为列表页）
    r = transport.get_binary(
        JWXT_BASE + PYFAXXWH_PATH + "?id=" + urllib.parse.quote(pyfaxx_id)
        + "&gnmkdm=" + PYFAWJ_GNM,
        headers={"Referer": JWXT_BASE + PYFAWJ_PATH + "?gnmkdm=" + PYFAWJ_GNM})
    if r.get("kind") == "network_error":
        return {"outcome": "network_error", "diag": r.get("err", "")}
    body = r.get("body") or b""
    status = r.get("status", 0)
    if status == 200 and body[:2] == b"PK":
        return {"outcome": "ok", "content_type": "application/msword",
                "size": len(body), "body": body}
    text = body.decode("utf-8", "replace")
    if any(m in text[:2000] for m in _LOGIN_MARKS):
        return {"outcome": "session_expired",
                "diag": "下载端点返回登录页（会话失效）"}
    if "出错啦" in text or "错误提示" in text:
        return {"outcome": "upstream_error",
                "diag": "jwglxt 业务拒绝（出错页；常见原因：id 非本会话有效值）"}
    if status != 200:
        return {"outcome": "http_error", "diag": "HTTP %s" % status}
    return {"outcome": "upstream_error",
            "diag": "响应非 docx（%d 字节，头 %r）" % (len(body), body[:8])}


def fetch_teaching_plans(transport: JwwebTransport, year: str) -> JwwebFetch:
    """F04 教学执行计划（N153540）：njdm_id=年级（如 2025）→ 该年级各专业执行计划。"""
    return _fetch_paged(transport, JXZXJH_PATH, JXZXJH_GNM, JXZXJH_PATH,
                        {"jg_id": "", "njdm_id": year, "dlbs": "", "zyh_id": "",
                         "currentPage_cx": "1"}, "items")


def fetch_teaching_plan_courses(transport: JwwebTransport, plan_ref: str) -> JwwebFetch:
    """F04 执行计划课程明细（实测 2026-10-14：软件工程 2025 → 77 门）。
    正确参数：jyxdxnm/jyxdxqm 留空（全部）+ jxzxjhxx_id=计划标识。"""
    return _fetch_paged(transport, JXZXJHKC_PATH, JXZXJH_GNM, JXZXJHKC_INDEX_PATH,
                        {"jyxdxnm": "", "jyxdxqm": "", "yxxdxnm": "", "yxxdxqm": "",
                         "shzt": "", "kch": "", "jxzxjhxx_id": plan_ref, "xdlx": ""},
                        "items", activate_index=False)


def fetch_free_classrooms(transport: JwwebTransport, dm: str, xqh_id: str = "2",
                          qssj: str = "", jssj: str = "", lh: str = "",
                          cdlb_id: str = "", cdmc: str = "") -> JwwebFetch:
    """F04 查询空闲教室（N2155）：dm=学期（如 2026-3）、xqh_id=校区（2=下沙）、
    日期范围/楼号/类别/名称可选。控件清单取证 2026-10-15。"""
    return _fetch_paged(transport, KXJS_PATH, KXJS_GNM, KXJS_PATH,
                        {"dm": dm, "xqh_id": xqh_id, "qssj": qssj, "jssj": jssj,
                         "lh": lh, "cdlb_id": cdlb_id, "cdmc": cdmc,
                         "qszws": "", "jszws": "", "cdejlb_id": ""}, "items",
                        activate_index=False)


def fetch_current_term(transport: JwwebTransport) -> dict:
    """长期入口①：当前学期（N2151 学期下拉的 **selected 选中项**=当前学期）。
    注意：下拉第一项可能是未来学期（实测 2031-2032 在最前）——不能用第一项。"""
    r = transport.get(JWXT_BASE + "/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151",
                      headers=_common_headers())
    if r.get("kind") == "network_error" or r.get("status") != 200:
        return {"outcome": "http_error", "term": None, "terms": [], "diag": "Index 获取失败"}
    page = r.get("text") or ""
    if "统一身份认证" in page[:2000]:
        return {"outcome": "session_expired", "term": None, "terms": [], "diag": "会话失效"}
    m = re.search(r'<select[^>]*id="xnm"[^>]*>([\s\S]*?)</select>', page)
    if not m:
        return {"outcome": "parse_error", "term": None, "terms": [],
                "diag": "学期下拉解析为空"}
    opts = re.findall(r'<option[^>]*value="(\d{4})"[^>]*>([^<]{4,12})', m.group(1))
    # selected 可在 value 前或后（<option selected value=...> / <option value=... selected>）
    sel = re.findall(r'<option[^>]*value="(\d{4})"[^>]*selected|'
                     r'<option[^>]*selected[^>]*value="(\d{4})"', m.group(1))
    sel = [a or b for a, b in sel]
    # R55：xqm（学期码）selected 同步解析——不猜值
    term_xqm = None
    m2 = re.search(r'<select[^>]*id="xqm"[^>]*>([\s\S]*?)</select>', page)
    if m2:
        sel2 = re.findall(r'<option[^>]*value="(\d{1,2})"[^>]*selected', m2.group(1))
        if sel2:
            term_xqm = sel2[0]
    if not opts:
        return {"outcome": "parse_error", "term": None, "terms": [],
                "diag": "学期下拉解析为空"}
    # R55 残余修：**只认 selected**——无 selected 受控失败（不按本机年份/第一项猜）
    if not sel:
        return {"outcome": "parse_error", "term": None, "term_xqm": None,
                "terms": opts[:10],
                "diag": "来源学年下拉无 selected 项——请显式传 xnm/xqm"}
    term = sel[0]
    return {"outcome": "ok", "term": term, "term_xqm": term_xqm,
            "terms": opts[:10], "diag": ""}


def resolve_class_bh_id(transport: JwwebTransport, class_name: str) -> dict:
    """长期入口②：班级名称→bh_id（N214505 Index 班级下拉 3743 项服务端渲染，
    每学期动态更新；精确名匹配，多命中返回候选）。"""
    r = transport.get(JWXT_BASE + "/jwglxt/kbdy/bjkbdy_cxBjkbdyIndex.html?gnmkdm=N214505",
                      headers=_common_headers())
    if r.get("kind") == "network_error" or r.get("status") != 200:
        return {"outcome": "http_error", "bh_id": None, "candidates": [],
                "diag": "Index 获取失败"}
    page = r.get("text") or ""
    if "统一身份认证" in page[:2000]:
        return {"outcome": "session_expired", "bh_id": None, "candidates": [],
                "diag": "会话失效"}
    m = re.search(r'<select[^>]*id="bh_id"[^>]*>([\s\S]*?)</select>', page)
    if not m:
        return {"outcome": "parse_error", "bh_id": None, "candidates": [],
                "diag": "班级下拉解析为空"}
    opts = re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]{1,40})', m.group(1))
    exact = [(k, v.strip()) for k, v in opts if v.strip() == class_name.strip()]
    if len(exact) == 1:
        return {"outcome": "ok", "bh_id": exact[0][0], "candidates": [exact[0]],
                "diag": ""}
    if len(exact) > 1:
        return {"outcome": "ambiguous", "bh_id": None, "candidates": exact,
                "diag": "班级名命中 %d 项，请用更精确名称" % len(exact)}
    fuzzy = [(k, v.strip()) for k, v in opts if class_name.strip() in v][:10]
    if fuzzy:
        return {"outcome": "ambiguous", "bh_id": None, "candidates": fuzzy,
                "diag": "精确名未命中，模糊候选 %d 项" % len(fuzzy)}
    return {"outcome": "not_found", "bh_id": None, "candidates": [],
            "diag": "班级下拉 %d 项中未找到「%s」" % (len(opts), class_name)}


def fetch_exam_registrations(transport: JwwebTransport) -> JwwebFetch:
    """F05/F11：考级项目报名状态（N2510 kjbm 模块，学生端本人视角）。
    接口实测 2026-10-15：POST kjbm_cxXskjbm.html?xmlbfl=1001&doType=query
    （空 items=当前无报名记录——真实空状态）。"""
    return _fetch_paged(transport, "/jwglxt/kjgl/kjbm_cxXskjbm.html", "N2510",
                        "/jwglxt/kjgl/kjbm_cxXskjbm.html?xmlbfl=1001",
                        {"xmlbfl": "1001"}, "items", activate_index=False)


FUNCDATA_GUID_COURSE = "4C5954FB1EA403CEE053018A0B0A63C0"  # B01-03 课程信息（校内取证 2026-10-15）
FUNCDATA_GUID_PRESELECT = "67F60C4AE5884749E053018A0B0A4F8A"  # B01-04 预选信息（校内取证 2026-10-15）


def fetch_preselect_info(transport: JwwebTransport, xnm: str, xqm: str,
                         page: int = 1, page_size: int = 15) -> JwwebFetch:
    """B01-04：学生预选信息查询（N255585 viewFunc 设计器数据接口）。

    接口（校内取证 2026-10-15）：POST design/funcData_cxFuncDataList.html
    ?func_widget_guid=<GUID>&gnmkdm=N255585——必须带 xnm/xqm；
    返回本人预选课程记录（课程/学分/学院/专业）。
    """
    transport.get("https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                  "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N255585&layout=default",
                  headers=_common_headers())
    form = {"xnm": xnm, "xqm": xqm, "_search": "false",
            "queryModel.showCount": str(page_size),
            "queryModel.currentPage": str(page)}
    all_items: list[dict] = []
    total_expected = None
    max_pages = 5  # 预选记录通常个位数——防御上限
    for pg in range(1, max_pages + 1):
        form["queryModel.currentPage"] = str(pg)
        r = transport.post_form(
            "https://jwxt.zjgsu.edu.cn/jwglxt/design/funcData_cxFuncDataList.html"
            "?func_widget_guid=" + FUNCDATA_GUID_PRESELECT + "&gnmkdm=N255585",
            form, headers=_common_headers())
        outcome, diag, data = classify_jwweb(r, list_keys=("items",))
        if outcome not in ("ok", "ok_empty"):
            if all_items:
                return JwwebFetch("partial_source", all_items,
                                  "第%d页起读取失败（%s）" % (pg, diag),
                                  len(all_items), total_expected=total_expected)
            return JwwebFetch(outcome, None, diag, None)
        chunk = (data.get("items") or []) if isinstance(data, dict) else []
        try:
            total_expected = int(data.get("totalResult"))
        except (TypeError, ValueError):
            total_expected = None
        all_items.extend(chunk)
        # R66：来源声明条数已取满 → 停止（否则翻页直到取满/空页）
        if total_expected is not None and len(all_items) >= total_expected:
            break
        if not chunk:
            break
    # R66：total>取到 → partial 缺口（不 ok 无缺页）
    if (total_expected is not None and len(all_items) < total_expected):
        return JwwebFetch("partial_source", all_items,
                          "来源声明 %s 条，实取 %d 条（缺页）"
                          % (total_expected, len(all_items)), len(all_items),
                          total_expected=total_expected)
    outcome = "ok_empty" if not all_items else "ok"
    return JwwebFetch(outcome, all_items, diag, len(all_items),
                      total_expected=total_expected)


def fetch_course_catalog(transport: JwwebTransport, xnm: str, xqm: str,
                         page: int = 1, page_size: int = 15) -> JwwebFetch:
    """B01-03：全校课程信息查询（N151066 viewFunc 设计器数据接口）。

    接口（校内取证 2026-10-15）：POST design/funcData_cxFuncDataList.html
    ?func_widget_guid=<GUID>&gnmkdm=N151066——**必须带 xnm/xqm**（否则 0 条）；
    来源不支持课程名筛选参数（前端筛选），keyword 由 service 本地过滤。
    """
    transport.get("https://jwxt.zjgsu.edu.cn/jwglxt/design/"
                  "viewFunc_cxDesignFuncPageIndex.html?gnmkdm=N151066&layout=default",
                  headers=_common_headers())
    form = {"xnm": xnm, "xqm": xqm, "_search": "false",
            "queryModel.showCount": str(page_size),
            "queryModel.currentPage": str(page)}
    r = transport.post_form(
        "https://jwxt.zjgsu.edu.cn/jwglxt/design/funcData_cxFuncDataList.html"
        "?func_widget_guid=" + FUNCDATA_GUID_COURSE + "&gnmkdm=N151066",
        form, headers=_common_headers())
    outcome, diag, data = classify_jwweb(r, list_keys=("items",))
    if outcome not in ("ok", "ok_empty"):
        return JwwebFetch(outcome, None, diag, None)
    total_expected = None
    try:
        total_expected = int(data.get("totalResult"))
    except (TypeError, ValueError):
        pass
    items = (data.get("items") or []) if isinstance(data, dict) else []
    return JwwebFetch(outcome, items, diag, len(items),
                      total_expected=total_expected)


def fetch_class_timetable(transport: JwwebTransport, bh_id: str,
                          xnm: str, xqm: str) -> JwwebFetch:
    "班级课表查询：班级标识从当前学期的Index下拉解析，不复用历史动态标识。"
    return _fetch_paged(transport, BJKB_PATH, BJKB_GNM, BJKB_PATH,
                        {"xnm": xnm, "xqm": xqm, "bh_id": bh_id,
                         "xqh_id": "2"}, "kbList", activate_index=False)


def fetch_innovation_scores(transport: JwwebTransport) -> JwwebFetch:
    """F04 创新创业获得分值（N4780）：POST Index?doType=query（无学期参数，本人全量）。"""
    return _fetch_paged(transport, SZF_PATH, SZF_GNM, SZF_PATH,
                        {}, "items", activate_index=False)


def fetch_syllabus_list(transport: JwwebTransport, xnm: str, xqm: str) -> JwwebFetch:
    """F04 授课提纲（N155002）：本人本学期有提纲的课程列表（jl_id 供正文查询后续）。"""
    return _fetch_paged(transport, SKDG_PATH, SKDG_GNM, SKDG_PATH,
                        {"xnm": xnm, "xqm": xqm, "jg_id": "", "kcmc": "",
                         "jxbmc": "", "rlzt": "", "sfsjk": "", "sfpkbz": ""}, "items")


def sanitize_url(url: str | None) -> tuple[str | None, bool]:
    """R41/P1：输出前净化链接——剥离认证类查询参数与 URI 用户信息。

    返回 (净化后 URL, 是否含被剥离的凭据)。无法解析的原始串原样返回并标记。
    """
    if not url:
        return url, False
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    dangerous = {"ticket", "token", "jsessionid", "sessionid", "sid", "auth",
                 "access_token", "st", "code", "sso", "casticket"}
    had_cred = False
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            had_cred = True
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = "%s:%d" % (netloc, parsed.port)
            parsed = parsed._replace(netloc=netloc)
        # R41 §2.52：;jsessionid 位于 urlsplit 的 params 段（非 path）——净化 params
        if parsed.params:
            psegs_all = parsed.params.split(";")
            psegs = [seg for seg in psegs_all
                     if not any(d in seg.lower() for d in dangerous)]
            if len(psegs) != len(psegs_all):
                had_cred = True
            parsed = parsed._replace(params=";".join(psegs))
        qs = parse_qsl(parsed.query, keep_blank_values=True)
        clean = [(k, v) for k, v in qs if k.lower() not in dangerous]
        if len(clean) != len(qs):
            had_cred = True
        # R41 残余：认证类 fragment（#token=.../#ticket=...）剥离
        frag = parsed.fragment
        if frag and any(d in frag.lower() for d in dangerous):
            had_cred = True
            parsed = parsed._replace(fragment="")
        return urlunparse(parsed._replace(query=urlencode(clean))), had_cred
    except Exception:
        # R41 残余：解析异常不回传原串（可能含凭据）——返回 None 由调用方具名
        return None, True


def fetch_online_class_urls(transport: JwwebTransport, xnm: str, xqm: str) -> JwwebFetch:
    """F04 网上上课地址（N1598）：xnm/xqm 必填，其余筛选空。
    R41：wsskdz/附件路径在入库前净化（存储与响应均为净化值）。"""
    fr = _fetch_paged(transport, WSKCDZ_PATH, WSKCDZ_GNM, WSKCDZ_PATH,
                      {"xnm": xnm, "xqm": xqm, "kkbm": "", "kch": "", "jgh": "",
                       "jxbmc": "", "kklxdm": "", "kclbdm": "", "kcgsdm": "",
                       "kcxzdm": "", "xqj": "", "zcd": "", "jcd": ""}, "items")
    if fr.items:
        cred_rows = 0
        for row in fr.items:
            for fld in ("wsskdz", "wsskfjlj"):
                raw = row.get(fld)
                if not raw:
                    continue
                clean, had = sanitize_url(raw)
                row[fld] = clean
                if had:
                    cred_rows += 1
        if cred_rows:
            fr.extras["sanitized_links"] = cred_rows
    return fr


def fetch_retake_courses(transport: JwwebTransport, cxxnm: str, cxxqm: str) -> JwwebFetch:
    """重修课程查询（N255720）：cxxnm/cxxqm=重修开设学年学期（如 2026/3）。"""
    return _fetch_paged(transport, CXKCCX_PATH, CXKCCX_GNM, CXKCCX_PATH,
                        {"cxxnm": cxxnm, "cxxqm": cxxqm, "kkbm_id": "", "kclbdm": "",
                         "bxqsfkk_cx": "", "kch": ""}, "items")


def fetch_internship_grades(transport: JwwebTransport, xnm: str, xqm: str) -> JwwebFetch:
    """F03 实习成绩（N544030）。本人 2023-2026 各学期实测合法空；接口链路已验证。"""
    return _fetch_paged(transport, SX_PATH, SX_GNM, SX_INDEX_PATH,
                        {"xnm": xnm, "xqm": xqm, "zmc": "", "jg_id": ""}, "items")


def fetch_grade_components(transport: JwwebTransport, xnm: str, xqm: str,
                           kch_id: str, jxb_id: str) -> JwwebFetch:
    """F03 明细第二步：单课程项目成绩（平时/期末/总评，原文含权重）。

    activate_index=False：同功能码会话已由课程列表步骤激活（实测 2026-10-14）。
    """
    return _fetch_paged(transport, GRADE_XM_PATH, GRADE_MX_GNM, GRADE_MX_INDEX_PATH,
                        {"xnm": xnm, "xqm": xqm, "kch_id": kch_id, "jxb_id": jxb_id},
                        "items", activate_index=False)


def _map_items(items: list[dict], field_map: dict,
               require_course_name: bool = True) -> tuple[list[dict] | None, list[str]]:
    """白名单映射；整批拒绝（不产出半成品），风格与 schema.validate_and_map 一致。

    R23：字段值类型检查（仅接受 str/int/float/None），类型不符在写缓存前拒绝。
    entry_id：来源无稳定 id 时用内容确定性哈希（同内容同 id，供本地游标/缓存引用）。
    """
    import hashlib
    out, violations = [], []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            violations.append("#%d: 非对象条目" % i)
            continue
        row = {}
        filled = set()
        bad_type = False
        for src, dst in field_map.items():
            v = it.get(src)
            if v is not None and (isinstance(v, bool)
                                  or not isinstance(v, (str, int, float))):
                # bool 是 int 子类，显式拒绝（R23 剩余）
                violations.append("#%d: %s 类型不合规(%s)" % (i, src, type(v).__name__))
                bad_type = True
                break
            val = v if v not in ("", None) else None
            # R64：同目标多别名（xmmc/xm→project_name）——首个有效值优先，
            # 后续别名缺 None 不覆盖已填充的有效值
            if dst in filled:
                if val is not None and row.get(dst) is None:
                    row[dst] = val
                continue
            row[dst] = val
            if val is not None:
                filled.add(dst)
        if bad_type:
            continue
        # R21/R23 剩余：整数课程名明确归一为字符串（不让 SDK 输出校验才崩）
        if isinstance(row.get("course_name"), (int, float)):
            row["course_name"] = str(row["course_name"])
        if require_course_name and not row.get("course_name"):
            violations.append("#%d: 缺少课程名" % i)
            continue
        digest_src = "|".join(str(it.get(k, "")) for k in sorted(it.keys()))
        row["entry_id"] = "jwglxt-" + hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:16]
        row["source_id"] = "src.jwglxt"
        out.append(row)
    if violations:
        return None, violations
    return out, []
