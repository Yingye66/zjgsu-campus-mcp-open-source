# -*- coding: utf-8 -*-
"""B01-12：mypage 教师主页适配器（公开源，无登录）。

接口链（实测 2026-10-14 23:00）：
- 搜索：POST https://mypage.zjgsu.edu.cn/_wp3services/generalQuery?queryObj=teacherHome
  body（UTF-8 urlencoded）：pageIndex/rows/conditions（六字段 or-like：
  discipline/department/title/career/education/exField1 + language/published）/
  orders/returnInfos/articleType=1&level=0&pageEvent=doSearchByPage
- 响应：{total, data: [{title 姓名, cnUrl 主页, career 职称, department 学院,
  headerPic 照片(含 token——输出前净化), education, visitCount, ...}], pageCount}
- 教师主页正文：cnUrl GET（结构化字段后续接入；本轮返回主页 URL）。

边界：公开源，按需查询单位/姓名，不做全校批量导出。
"""
import io
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin

import requests

MYPAGE_BASE = "https://mypage.zjgsu.edu.cn"
GENERAL_QUERY = MYPAGE_BASE + "/_wp3services/generalQuery?queryObj=teacherHome"


@dataclass
class MypageFetch:
    outcome: str                # ok / ok_empty / http_error / network_error / parse_error
    items: list
    diag: str
    raw_count: int | None = None


def _sanitize_url(url):
    """R41/P1：净化链接（认证参数/URI 用户信息）。返回 (净化值, 是否含凭据)。"""
    if not url:
        return url, False
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    dangerous = {"ticket", "token", "jsessionid", "sessionid", "sid", "auth",
                 "access_token", "st", "sso", "casticket"}
    had_cred = False
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            had_cred = True
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = "%s:%d" % (netloc, parsed.port)
            parsed = parsed._replace(netloc=netloc)
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
        frag = parsed.fragment
        if frag and any(d in frag.lower() for d in dangerous):
            had_cred = True
            parsed = parsed._replace(fragment="")
        return urlunparse(parsed._replace(query=urlencode(clean))), had_cred
    except Exception:
        return None, True


def search_teachers(keyword: str, limit: int = 20) -> MypageFetch:
    """按姓名/职称等关键词搜索教师（六字段 or-like，按热度排序）。

    R47/R48：按来源分页有界获取（rows=min(limit,50)/页）；错误对象≠合法空；
    坏行具名不静默。"""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                         "Origin": MYPAGE_BASE,
                         "Referer": MYPAGE_BASE + "/jscx/list.htm"})
    rows_per_page = max(1, min(int(limit), 50))
    conditions = json.dumps([
        {"orConditions": [
            {"field": "discipline", "value": keyword, "judge": "like"},
            {"field": "department", "value": keyword, "judge": "like"},
            {"field": "title", "value": keyword, "judge": "like"},
            {"field": "career", "value": keyword, "judge": "like"},
            {"field": "education", "value": "%" + keyword + "%", "judge": "like"},
            {"field": "exField1", "value": keyword, "judge": "like"},
        ]},
        {"field": "language", "value": "1", "judge": "="},
        {"field": "published", "value": "1", "judge": "="},
    ], ensure_ascii=False)
    orders = json.dumps([{"field": "hot", "type": "desc"}])
    return_infos = json.dumps([
        {"field": "title", "name": "title"},
        {"field": "cnUrl", "name": "cnUrl"},
        {"field": "career", "name": "career"},
        {"field": "headerPic", "name": "headerPic"},
        {"field": "department", "name": "department"},
        {"field": "education", "name": "education"},
        {"field": "finalEducation", "name": "finalEducation"},
        {"field": "ownDepartment", "name": "ownDepartment"},
        {"field": "visitCount", "name": "visitCount"},
        {"field": "discipline", "name": "discipline"},
        {"field": "exField1", "name": "exField1"},
    ])

    items = []
    bad_rows = 0
    cred_rows = 0
    total_seen = None
    page_index = 1
    max_pages = 5  # 有界：limit<=100/rows>=20 → 最多 5 页
    diag_parts = []
    while page_index <= max_pages:
        body = ("pageIndex=%d&rows=%d&conditions=%s&orders=%s&returnInfos=%s"
                "&articleType=1&level=0&pageEvent=doSearchByPage"
                % (page_index, rows_per_page, quote(conditions, safe=""),
                   quote(orders, safe=""), quote(return_infos, safe="")))
        try:
            r = sess.post(GENERAL_QUERY, data=body.encode("utf-8"), timeout=20)
        except requests.RequestException as e:
            return MypageFetch("network_error", None, "网络错误（%s）" % e, None)
        if r.status_code != 200:
            return MypageFetch("http_error", None, "HTTP %s" % r.status_code, None)
        r.encoding = "utf-8"
        try:
            data = json.loads(r.text)
        except Exception:
            return MypageFetch("parse_error", None, "响应非 JSON", None)
        # R48：错误对象≠合法空
        if isinstance(data, dict) and data.get("result") is False:
            return MypageFetch("upstream_error", None,
                               "来源错误响应（%s）" % (data.get("error") or "?"), None)
        if not isinstance(data, dict):
            return MypageFetch("parse_error", None, "响应根非对象", None)
        rows = data.get("data")
        if rows is None:
            return MypageFetch("parse_error", None, "响应缺 data 字段", None)
        if not isinstance(rows, list):
            return MypageFetch("parse_error", None,
                               "data 非列表（%s）" % type(rows).__name__, None)
        total_seen = data.get("total")
        for row in rows:
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            if not str(row.get("title") or "").strip():
                bad_rows += 1   # 姓名缺失为核心字段违约
                continue
            url, had = _sanitize_url(row.get("cnUrl"))
            pic, had2 = _sanitize_url(row.get("headerPic"))
            if had or had2:
                cred_rows += 1
            items.append({
                "name": str(row.get("title") or "").strip(),
                "page_url": url,
                "career": str(row.get("career") or "").strip() or None,
                "department": str(row.get("department") or "").strip() or None,
                "education": str(row.get("education") or "").strip() or None,
                "own_department": str(row.get("ownDepartment") or "").strip() or None,
                "discipline": str(row.get("discipline") or "").strip() or None,
                "visit_count": str(row.get("visitCount") or "").strip() or None,
                "photo_url": pic,
            })
        # 分页：total 已读完或本页不足则停
        try:
            total_n = int(total_seen)
        except (TypeError, ValueError):
            total_n = None
        if total_n is not None and len(items) >= total_n:
            break
        if len(rows) < rows_per_page:
            break
        page_index += 1

    if bad_rows:
        diag_parts.append("%d 行结构/姓名违约已具名跳过" % bad_rows)
    if cred_rows:
        diag_parts.append("%d 条照片/链接含认证类参数已净化" % cred_rows)
    # R47：来源未读全（total 多于已取）→ partial 语义由 service 依据 total 判定；
    # diag 带读取边界
    if total_n is not None and len(items) < total_n:
        diag_parts.append("来源声明 %d 条，实取 %d 条" % (total_n, len(items)))
    # R48 §2.62：正总数与空页矛盾不得算合法空——先做总数对账
    total_mismatch = (total_n is not None and total_n > 0
                      and not items and not bad_rows)
    if total_mismatch:
        diag_parts.insert(0, "来源声明 total=%d 但读到空页（协议矛盾）" % total_n)
        return MypageFetch("partial_source", items, "；".join(diag_parts), total_seen)
    outcome = "ok_empty" if not items and not bad_rows and total_n in (0, None) else (
        "partial_source" if (bad_rows or (total_n is not None and len(items) < total_n))
        else "ok")
    return MypageFetch(outcome, items, "；".join(diag_parts), total_seen)


def fetch_teacher_home_text(page_url: str) -> MypageFetch:
    """读取教师主页正文文本（公开页 GET）。page_url 须为搜索返回的 cnUrl。"""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    if not page_url.startswith("http"):
        page_url = urljoin(MYPAGE_BASE + "/", page_url)
    try:
        r = sess.get(page_url, timeout=20)
    except requests.RequestException as e:
        return MypageFetch("network_error", None, "网络错误（%s）" % e, None)
    if r.status_code != 200:
        return MypageFetch("http_error", None, "HTTP %s" % r.status_code, None)
    r.encoding = "utf-8"
    body = re.sub(r"<script[\s\S]*?</script>", "", r.text)
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"(&nbsp;|\s)+", " ", body).strip()
    return MypageFetch("ok", [{"text": body[:3000], "url": page_url}],
                       "", 1)

def fetch_campus_services(transport, fwdxid: str, app_cookies=None):
    """F09/B01-10：服务中心完整服务目录（App API，App 域 cookie 鉴权）。

    接口取证 2026-10-14：POST my.zjgsu.edu.cn/api/service/all（fwdxid=本科生，
    查询型 POST 无状态变更）；响应 {data: [76 服务]}。字段：fwid/fwmc/fwlbmcs/
    blmsmc/fwdz/blcs/fwpf 等。app_cookies 来自登录会话（§2.78 App 域顺带建立）。
    """
    import json as _json
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "X-Requested-With": "XMLHttpRequest"})
    for c in (app_cookies or []):
        sess.cookies.set(c["name"], c["value"],
                         domain=c.get("domain") or "my.zjgsu.edu.cn",
                         path=c.get("path") or "/")
    try:
        r = {"kind": "response", "status": None, "text": None, "url": ""}
        _r = sess.post("https://my.zjgsu.edu.cn/api/service/all",
                       data={"fwdxid": fwdxid}, timeout=20)
        r = {"kind": "response", "status": _r.status_code, "text": _r.text,
             "url": _r.url}
    except Exception as e:
        return {"outcome": "network_error", "items": None, "diag": "网络错误（%s）" % e}
    if r.get("status") in (401, 403) or "login" in (r.get("url") or ""):
        return {"outcome": "upstream_error", "items": None,
                "diag": "App 域未登录（需重跑 login_cli init 建立 App 会话）"}
    # R58 §2.76 同款协议校验：根/data/list 类型（nonobject_root 等 case）
    try:
        data = _json.loads(r.get("text") or "")
    except Exception:
        return {"outcome": "parse_error", "items": None, "diag": "响应非 JSON"}
    if not isinstance(data, dict):
        return {"outcome": "parse_error", "items": None,
                "diag": "响应根非对象（%s）" % type(data).__name__}
    d = data.get("data")
    if d is None:
        return {"outcome": "parse_error", "items": None, "diag": "响应缺 data"}
    if not isinstance(d, list):
        return {"outcome": "parse_error", "items": None,
                "diag": "data 非列表（%s）" % type(d).__name__}
    if r.get("kind") == "network_error":
        return {"outcome": "network_error", "items": None, "diag": "网络错误"}
    if r.get("status") != 200:
        return {"outcome": "http_error", "items": None, "diag": "HTTP %s" % r.get("status")}
    text = (r.get("text") or "").strip()
    try:
        data = _json.loads(text)
    except Exception:
        return {"outcome": "parse_error", "items": None, "diag": "响应非 JSON"}
    if not isinstance(data, dict) or data.get("error_code") not in (0, "0", None):
        return {"outcome": "upstream_error", "items": None,
                "diag": "业务错误（error_code=%s）" % data.get("error_code")}
    rows = data.get("data")
    if rows is None:
        return {"outcome": "parse_error", "items": None, "diag": "响应缺 data"}
    if not isinstance(rows, list):
        return {"outcome": "parse_error", "items": None,
                "diag": "data 非列表（%s）" % type(rows).__name__}
    # R58 非阻断：合法空（data=[]）→ ok_empty（empty_evidence 语义正确）
    return {"outcome": ("ok_empty" if not rows else "ok"),
            "items": rows, "diag": ""}


NOTICE_LIST_URL = "http://news.zjgsu.edu.cn/18/"


def fetch_public_notices(transport=None):
    """B01-11：学校官网公开通知列表（news.zjgsu.edu.cn/18/，匿名 GET）。

    结构取证 2026-10-15：<li><a href="/18/view_N.html" title=标题>标题</a>
    <span class="fr">yyyy-MM-dd</span></li>。只读公开源；不替代个人收件箱。
    """
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        r = sess.get(NOTICE_LIST_URL, timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "items": None, "diag": "网络错误（%s）" % e}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None,
                "diag": "HTTP %s" % r.status_code}
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    items = []
    bad_rows = 0
    pat = re.compile(
        r'<a href="(/18/view_\d+\.html)"[^>]*title="([^"]*)"[^>]*>[^<]*</a>'
        r'[\s\S]{0,200}?(\d{4}-\d{2}-\d{2})')
    for m in pat.finditer(html):
        url_rel, title, date = m.group(1), m.group(2).strip(), m.group(3)
        if not title:
            bad_rows += 1
            continue
        items.append({
            "title": title,
            "url": NOTICE_LIST_URL.rsplit("/", 2)[0] + url_rel if url_rel.startswith("/")
                   else url_rel,
            "date": date,
        })
    if not items:
        # 宽松回退：标题属性缺失时取锚文本
        pat2 = re.compile(
            r'<a href="(/18/view_\d+\.html)"[^>]*>([^<]{4,80})</a>'
            r'[\s\S]{0,200}?(\d{4}-\d{2}-\d{2})')
        for m in pat2.finditer(html):
            url_rel, title, date = m.group(1), m.group(2).strip(), m.group(3)
            items.append({"title": title, "url": url_rel, "date": date})
    if not items:
        return {"outcome": "parse_error", "items": None,
                "diag": "通知条目解析为空（结构变化）", "bad_rows": bad_rows}
    # R54：实际 HTML 坏行进 outcome（混合空 title/缺 title → partial 不静默漏条）
    diag = ("%d 行缺标题已具名跳过" % bad_rows) if bad_rows else ""
    outcome = "partial_source" if (bad_rows and items) else "ok"
    return {"outcome": outcome, "items": items, "diag": diag,
            "bad_rows": bad_rows}


JOB_FAIR_SEARCH = "https://jyw.zjgsu.edu.cn/career//zphxx/search/{page}/10"


def fetch_job_fairs(transport=None):
    """B01 招聘会列表（jyw 智慧就业平台，匿名公开源）。

    接口取证 2026-10-15：POST /career//zphxx/search/{page}/10
    （**页码在 URL 路径**，body=zphzt=&jbrqFlag=10；rows 固定 10/页）。
    total=113 场。状态判定纪律：**举办状态以详情页邀请函正文为准，不自动推断**。
    """
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                         "Origin": "https://jyw.zjgsu.edu.cn",
                         "Referer": "https://jyw.zjgsu.edu.cn/career/zphxx"})
    items = []
    bad_rows = 0
    total_seen = None
    max_pages = 13  # 113 场 / 10 每页 = 12 页（有界余量）
    for pg in range(1, max_pages + 1):
        try:
            r = sess.post(JOB_FAIR_SEARCH.format(page=pg),
                          data="zphzt=&jbrqFlag=10".encode("utf-8"), timeout=20)
        except Exception as e:
            return {"outcome": "network_error", "items": None,
                    "diag": "网络错误（%s）" % e, "total": None}
        if r.status_code != 200:
            return {"outcome": "http_error", "items": None,
                    "diag": "HTTP %s" % r.status_code, "total": None}
        r.encoding = "utf-8"
        try:
            data = json.loads(r.text)
        except Exception:
            return {"outcome": "parse_error", "items": None,
                    "diag": "响应非 JSON（第 %d 页）" % pg, "total": None}
        # R56：业务 code 校验（HTTP 200 但 code 500 等 ≠ 合法空）
        if isinstance(data, dict) and data.get("code") not in (200, "200", None):
            return {"outcome": "upstream_error", "items": None,
                    "diag": "业务错误（code=%s）" % data.get("code"), "total": None}
        d = data.get("data") if isinstance(data, dict) else None
        if d is None:
            return {"outcome": "parse_error", "items": None,
                    "diag": "响应缺 data（第 %d 页）" % pg, "total": None}
        if not isinstance(d, dict):
            return {"outcome": "parse_error", "items": None,
                    "diag": "data 非对象（%s）" % type(d).__name__, "total": None}
        rows = d.get("list")
        if rows is None:
            return {"outcome": "parse_error", "items": None,
                    "diag": "响应缺 list（第 %d 页）" % pg, "total": None}
        if not isinstance(rows, list):
            return {"outcome": "parse_error", "items": None,
                    "diag": "list 非列表（%s）" % type(rows).__name__, "total": None}
        # 非法 total（"broken" 等）→ None（不当可信值）
        try:
            total_seen = int(d.get("total")) if d.get("total") is not None else None
        except (TypeError, ValueError):
            total_seen = None
        if total_seen is None:
            try:
                total_seen = int(d.get("total"))
            except (TypeError, ValueError):
                total_seen = None
        if not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                bad_rows += 1
                continue
            title = str(row.get("zphzt") or "").strip()
            if not title:
                bad_rows += 1
                continue
            items.append({
                "title": title,
                "fair_type": str(row.get("zphlbmc") or "").strip() or None,
                "start_time": str(row.get("jbkssj") or "").strip() or None,
                "end_time": str(row.get("jbjssj") or "").strip() or None,
                "location": str(row.get("jbdd") or "").strip() or None,
                "signup_deadline": str(row.get("bmjzsj") or "").strip() or None,
                "invited_orgs": str(row.get("bmdws") or "").strip() or None,
                "fair_id": str(row.get("zphid") or "").strip() or None,
            })
        if total_seen is not None and len(items) >= total_seen:
            break
    diag = ""
    if bad_rows:
        diag = "%d 行结构异常已具名跳过" % bad_rows
    outcome = "ok_empty" if not items and total_seen in (0, None) else (
        "partial_source" if bad_rows or (total_seen and len(items) < total_seen) else "ok")
    return {"outcome": outcome, "items": items, "diag": diag, "total": total_seen}


CAREER_CATEGORIES = {
    # 代码: (中文标签, search 模式)  全部 2026-10-15 实测匿名可读
    "zpxx": ("全职招聘", "zpxx"),
    "sxzpxx": ("实习招聘", "zpxx"),
    "zpgg": ("招聘公告", "news"),
    "zygh": ("职业规划", "news"),
    "xyfc": ("学院风采", "news"),
    "zprd": ("招聘热点", "news"),
    "zcty": ("职场体验", "news"),
    "syxx": ("生源速览", "news"),
    "lxzpzn": ("招聘指南", "news"),
    "zyghds": ("职业生涯规划大赛", "news"),
}


def fetch_career_info(category: str, keyword: str = "", page: int = 1):
    """F32/B01-11：智慧就业网通用栏目查询（匿名公开源）。

    接口模式（2026-10-15 全栏目实测）：
    - zpxx 系：POST /career//zpxx/search/{category}
    - news 系：POST /career//news/search/{category}
    响应 {code:200, data:{total, list:[...]}}。
    """
    import requests
    if category not in CAREER_CATEGORIES:
        return {"outcome": "upstream_error", "items": None, "diag": "未知栏目代码",
                "total": None}
    label, mode = CAREER_CATEGORIES[category]
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                         "Origin": "https://jyw.zjgsu.edu.cn",
                         "Referer": "https://jyw.zjgsu.edu.cn/career/index"})
    if mode == "zpxx":
        url = "https://jyw.zjgsu.edu.cn/career//zpxx/search/%s" % category
        body = "pageIndex=%d&pageSize=10" % page
        if keyword:
            body += "&zpxxmc=" + quote(keyword)
    else:
        url = "https://jyw.zjgsu.edu.cn/career//news/search/%s" % category
        body = "pageIndex=%d&pageSize=10" % page
        if keyword:
            body += "&newstitle=" + quote(keyword)
    try:
        r = sess.post(url, data=body.encode("utf-8"), timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "items": None, "diag": "网络错误（%s）" % e,
                "total": None}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None, "diag": "HTTP %s" % r.status_code,
                "total": None}
    r.encoding = "utf-8"
    try:
        data = json.loads(r.text)
    except Exception:
        return {"outcome": "parse_error", "items": None, "diag": "响应非 JSON",
                "total": None}
    # R58 §2.76 同款协议校验：业务失败/根类型/data 类型/total 类型
    if isinstance(data, dict) and data.get("result") is False:
        return {"outcome": "upstream_error", "items": None,
                "diag": "来源错误（%s）" % data.get("error"), "total": None}
    if not isinstance(data, dict):
        return {"outcome": "parse_error", "items": None,
                "diag": "响应根非对象（%s）" % type(data).__name__, "total": None}
    d = data.get("data")
    if d is None:
        return {"outcome": "parse_error", "items": None, "diag": "响应缺 data",
                "total": None}
    if not isinstance(d, dict):
        return {"outcome": "parse_error", "items": None,
                "diag": "data 非对象（%s）" % type(d).__name__, "total": None}
    rows = d.get("list") or []
    if not isinstance(rows, list):
        return {"outcome": "parse_error", "items": None,
                "diag": "list 非列表（%s）" % type(rows).__name__, "total": None}
    total = d.get("total")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None  # "broken" 等→None（不当可信值）
    return {"outcome": "ok", "items": rows, "diag": "", "total": total}


def fetch_detail_text(url: str, max_len: int = 3000, allowed_hosts=None):
    """通用详情正文抓取（GET → 文本提取，3000 字内）。

    R63：allowed_hosts=目标域名白名单（输入与最终 URL 都校验）——拒绝非目标
    host/本机地址/重定向出域。通知/新闻/招聘会调用方各自传入。"""
    import requests
    from urllib.parse import urlparse
    hosts = [h.lower() for h in (allowed_hosts or [])]
    if hosts:
        try:
            _u = urlparse(url)
            if _u.hostname is None or _u.hostname.lower() not in hosts:
                return {"outcome": "upstream_error", "text": None,
                        "diag": "来源 host 不在白名单（%s）" % (_u.hostname or "无")}
        except Exception:
            return {"outcome": "upstream_error", "text": None,
                    "diag": "URL 解析失败"}
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    # R63 残余：**禁自动跟随重定向**——逐跳校验 Location/协议/host，再请求
    from urllib.parse import urlparse as _up
    current = url
    hops = 0
    r = None
    try:
        while hops <= 5:
            _pu = _up(current)
            if _pu.scheme not in ("http", "https"):
                return {"outcome": "upstream_error", "text": None,
                        "diag": "重定向协议不允许（%s）" % _pu.scheme}
            if hosts and (_pu.hostname is None or _pu.hostname.lower() not in hosts):
                return {"outcome": "upstream_error", "text": None,
                        "diag": "重定向 host 出域（%s）" % (_pu.hostname or "无")}
            r = sess.get(current, timeout=20, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location") or ""
                if not loc:
                    break
                current = loc if loc.startswith("http") else (
                    _pu.scheme + "://" + _pu.netloc + loc)
                hops += 1
                continue
            break
        else:
            return {"outcome": "upstream_error", "text": None, "diag": "重定向超 5 跳"}
    except Exception as e:
        return {"outcome": "network_error", "text": None, "diag": "网络错误（%s）" % e}
    if r is None or r.status_code != 200:
        return {"outcome": "http_error", "text": None,
                "diag": "HTTP %s" % (getattr(r, "status_code", None) if r else "?")}
    r.encoding = r.apparent_encoding or "utf-8"
    body = re.sub(r"<script[\s\S]*?</script>", "", r.text)
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"(&nbsp;|\s)+", " ", body).strip()
    if len(body) < 50:
        return {"outcome": "parse_error", "text": None, "diag": "正文过短（可能非详情页）"}
    return {"outcome": "ok", "text": body[:max_len], "diag": ""}


NEWS_CATEGORIES = {
    "2": "商大新闻", "3": "综合新闻", "17": "学术研究", "5": "媒体商大",
    "18": "通知公告", "4": "教学科研", "7": "理论学院", "24": "专题列表",
}
NEWS_BASE = "http://news.zjgsu.edu.cn"


def fetch_news_category(cat: str, page: int = 1):
    """新闻网栏目列表（匿名公开源；首页 /N/，历史翻页 /N/Page_N.html）。"""
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    if page <= 1:
        list_path = "/%s/" % cat
    else:
        list_path = "/%s/Page_%d.html" % (cat, page)
    try:
        r = sess.get(NEWS_BASE + list_path, timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "items": None,
                "diag": "网络错误（%s）" % e}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None,
                "diag": "HTTP %s" % r.status_code}
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    items = []
    # R59 修：日期可选（专题列表/部分栏目无相邻日期）
    pat = re.compile(
        r'<a href="(/%s/view_\d+\.html)"[^>]*title="([^"]*)"[^>]*>' % cat)
    for m in pat.finditer(html):
        # 尝试就近找日期（无则 None）
        seg = html[m.end():m.end() + 250]
        dm = re.search(r'(\d{4}-\d{2}-\d{2})', seg)
        items.append({"title": m.group(2).strip(),
                      "url": NEWS_BASE + m.group(1),
                      "date": dm.group(1) if dm else None})
    if not items:
        pat2 = re.compile(
            r'<a href="(/%s/view_\d+\.html)"[^>]*>([^<]{2,80})</a>' % cat)
        for m in pat2.finditer(html):
            seg = html[m.end():m.end() + 250]
            dm = re.search(r'(\d{4}-\d{2}-\d{2})', seg)
            items.append({"title": m.group(2).strip(),
                          "url": NEWS_BASE + m.group(1),
                          "date": dm.group(1) if dm else None})
    if not items:
        return {"outcome": "parse_error", "items": None, "diag": "条目解析为空"}
    return {"outcome": "ok", "items": items, "diag": ""}


def fetch_news_detail(url: str):
    """新闻网详情正文（GET → 文本 3000 字内；host 白名单 news.zjgsu.edu.cn）。"""
    return fetch_detail_text(url, allowed_hosts=["news.zjgsu.edu.cn"])


# ============ 图书馆（lib.zjgsu.edu.cn 公开站） ============
LIB_CATEGORIES = {
    "gzbj": ("馆藏布局", "/gzbj/list.htm"),
    "fgzd": ("规章制度", "/2024/0402/c1160a169421/page.htm"),
    "cjwt": ("常见问题", "/1098/list.htm"),
    "jyfw": ("借阅服务", "/1042/list.htm"),
    "szz": ("数字资源", "/1013/list.htm"),
    "zjz": ("自建资源", "/1014/list.htm"),
    "dsj": ("读书节", "/1072/list.htm"),
    "jsl": ("竞赛类", "/1073/list.htm"),
    "zll": ("展览类", "/1074/list.htm"),
    "hsfm": ("馆舍风貌", "/1157/list.htm"),
}
LIB_BASE = "http://lib.zjgsu.edu.cn"


def fetch_library_info(cat: str):
    """图书馆公开信息（馆藏布局全文/栏目列表）。"""
    import requests
    meta = LIB_CATEGORIES.get(cat)
    if not meta:
        return {"outcome": "upstream_error", "items": None,
                "diag": "未知栏目（%s）" % cat}
    label, path = meta
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        r = sess.get(LIB_BASE + path, timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "items": None,
                "diag": "网络错误（%s）" % e}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None,
                "diag": "HTTP %s" % r.status_code}
    r.encoding = r.apparent_encoding or "utf-8"
    body = re.sub(r"<script[\s\S]*?</script>", "", r.text)
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"(&nbsp;|\s)+", " ", body).strip()
    if len(body) < 100:
        return {"outcome": "parse_error", "items": None, "diag": "内容过短"}
    # 栏目列表型（常见问题等）提取条目；布局型直接全文
    # R61：条目提取按实际路径（含日期的 .htm 链接，不限栏目代码——路径形如 /2024/0402/...）
    items = []
    if cat in ("gzbj", "fgzd"):
        items.append({"title": label, "text": body[:4000]})
    else:
        links = re.findall(r'<a href="([^"]*c\d+a\d+\.htm|[^"]*/20\d{4}/[^"]*\.htm)"'
                           r'[^>]*>([^<]{6,60})</a>', r.text)
        seen = set()
        for u2, t2 in links:
            t2 = t2.strip()
            if u2 in seen or not t2:
                continue
            seen.add(u2)
            items.append({"title": t2, "url": LIB_BASE + u2, "date": None})
            if len(items) >= 30:
                break
        if not items:
            items.append({"title": label, "text": body[:3000]})
    return {"outcome": "ok", "items": items, "diag": "", "label": label}


def fetch_app_messages(app_cookies):
    """F08：my.zjgsu 门户消息/通知列表（App 域会话，本人视角）。

    流程：app_cookies → 门户首页（会话上下文）→ POST /api/notice/list。
    error_code=0 + res=INTERFACE_PLATFORM_EXCEPTION → 来源通知系统异常/空（如实）。
    """
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "X-Requested-With": "XMLHttpRequest",
                         "Referer": "https://my.zjgsu.edu.cn/"})
    for c in (app_cookies or []):
        sess.cookies.set(c["name"], c["value"], domain=c["domain"],
                         path=c["path"] or "/")
    try:
        r0 = sess.get(MY_BASE2 + "/", timeout=20)
        r = sess.post(MY_BASE2 + "/api/notice/list",
                      data={"pageNumber": "1", "pageSize": "20"}, timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "items": None,
                "diag": "网络错误（%s）" % e}
    if r.status_code == 203:
        return {"outcome": "upstream_error", "items": None,
                "diag": "App 会话失效（需重跑 login_cli init）"}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None,
                "diag": "HTTP %s" % r.status_code}
    r.encoding = "utf-8"
    try:
        data = json.loads(r.text)
    except Exception:
        return {"outcome": "parse_error", "items": None, "diag": "响应非 JSON"}
    # R65：根/data 类型校验
    if not isinstance(data, dict):
        return {"outcome": "parse_error", "items": None,
                "diag": "响应根非对象（%s）" % type(data).__name__}
    if data.get("result") is False or data.get("error_code") not in (0, "0"):
        return {"outcome": "upstream_error", "items": None,
                "diag": "App 业务错误（%s）" % data.get("message"), "items": None}
    d = data.get("data")
    if not isinstance(d, dict):
        return {"outcome": "parse_error", "items": None,
                "diag": "data 非对象（%s）" % type(d).__name__}
    res = d.get("res")
    # R65：来源接口异常 ≠ 合法空——upstream_error（保旧成功快照）
    if res == "INTERFACE_PLATFORM_EXCEPTION" or not isinstance(res, list):
        return {"outcome": "upstream_error", "items": None,
                "diag": "来源通知系统接口异常/未返回列表（%s）"
                        % (str(res)[:30] if res is not None else "空"), "items": None}
    items = []
    for row in res:
        if not isinstance(row, dict):
            continue
        _u = row.get("url") or row.get("link")
        u_clean = None
        if _u:
            from src.adapters.campus.mypage import _sanitize_url as _su
            u_clean, _h = _su(str(_u))
        items.append({
            "title": str(row.get("title") or row.get("name") or "").strip() or None,
            "content": str(row.get("content") or "").strip() or None,
            "date": str(row.get("date") or row.get("createTime") or "").strip() or None,
            "url": u_clean,
        })
    return {"outcome": "ok", "items": items, "diag": "", "bad_rows": 0}


MY_BASE2 = "https://my.zjgsu.edu.cn"


LIB_SEARCH_URL = "http://book.zjgsu.superlib.net/searchMix"


def fetch_library_search(keyword: str, search_type: str = "book"):
    """F21：图书馆馆藏检索（superlib 聚合检索，匿名公开源；UTF-8 实测）。

    search_type：book=图书（searchMix）/jour=期刊（searchJour）/thesis=学位论文。
    R67：diag_js 作用域修正；未知链接形态具名；书目 URL 净化（query 认证参数）。
    期刊/学位论文端点条目为 JS 渲染（如实 upstream_error）。
    """
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    base = {"book": LIB_SEARCH_URL,
            "jour": "http://jour.zjgsu.superlib.net/searchJour",
            "thesis": "http://jour.zjgsu.superlib.net/searchThesis"}.get(
        search_type, LIB_SEARCH_URL)
    try:
        r = sess.get(base, params={"sw": keyword}, timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "items": None,
                "diag": "网络错误（%s）" % e}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None,
                "diag": "HTTP %s" % r.status_code}
    r.encoding = "utf-8"   # superlib 页面实测 UTF-8
    html = r.text
    items = []
    if search_type == "book":
        links = re.findall(r'href="(http://book\.zjgsu\.superlib\.net/views/specific/'
                           r'\d+/bookDetail\.jsp[^"]*)"[^>]*>([^<]{2,60})</a>', html)
    elif search_type == "jour":
        links = re.findall(r'href="(/searchJour[^"]*)"[^>]*>'
                           r'([^<]{2,40}\(\d+\))<', html)
        links = [("http://jour.zjgsu.superlib.net" + u2, t2) for u2, t2 in links]
    else:
        links = []   # thesis 端点实测无条目结构（200 空壳——如实 upstream_error）
    seen = set()
    for u2, t2 in links:
        t2 = t2.strip()
        if u2 in seen or not t2:
            continue
        seen.add(u2)
        # R67：书目 URL 净化（bookDetail.jsp?dxNumber=... 的 query 认证参数）
        u_clean, h2 = _sanitize_url(u2)
        items.append({"title": t2, "url": u_clean})
    counts = (re.findall(r'(图书|期刊|学位论文|报纸)(\d+)种', html)
              or re.findall(r'(图书|期刊|学位论文|报纸)\s*(\d+)\s*种', html))
    counts = {k: v for k, v in counts}
    if search_type != "book" and not items:
        # R67：jour/thesis JS 渲染——upstream_error（不虚构空/不洗 ok）
        return {"outcome": "upstream_error", "items": None,
                "diag": "%s 端点条目为 JS 渲染（暂不可读）" % search_type}
    if not items and not counts:
        return {"outcome": "parse_error", "items": None,
                "diag": "检索结果解析为空（%s 端点当前无条目）" % search_type}
    # R67：正计数无条目 → 假空（counts>0 但没提取到 → partial/具名）
    if not items and counts:
        return {"outcome": "upstream_error", "items": None,
                "diag": "来源声明 %s 但条目提取为空（结构待适配）" % counts}
    return {"outcome": "ok", "items": items, "diag": "", "counts": counts}

def fetch_library_book_detail(detail_url: str, keyword: str = ""):
    """F21：书目详情（bookDetail 链接的 d= 签名与会话绑定——**先重新检索
    刷新同 dxNumber 的新签名链接**，再取详情）。"""
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        dx = re.search(r'dxNumber=([^&"]*)', detail_url)
        dx_no = dx.group(1) if dx else None
        # 1) 重新检索（keyword 优先）——**禁自动跟随重定向**（R68 逐跳）
        kws = [keyword] if keyword else []
        fresh_url = None
        for kw in kws:
            r0 = sess.get(LIB_SEARCH_URL, params={"sw": kw}, timeout=20,
                          allow_redirects=False)
            if r0.status_code != 200:
                continue
            r0.encoding = "utf-8"
            links = re.findall(r'href="(http://book\.zjgsu\.superlib\.net/views/'
                               r'specific/\d+/bookDetail\.jsp[^"]*)"', r0.text)
            # R68：dxNumber **精确比对**（dxNumber=<值>& 或行尾——不子串误匹配）
            if dx_no:
                for u2 in links:
                    if ("dxNumber=%s&" % dx_no) in u2 or u2.endswith(
                            "dxNumber=%s" % dx_no):
                        fresh_url = u2
                        break
            if fresh_url:
                break
        target = fresh_url or detail_url
        # 2) 逐跳取详情（R63 逐跳校验：book/www 同平台子域白名单）
        from urllib.parse import urlparse as _up
        hosts = ["book.zjgsu.superlib.net", "www.zjgsu.superlib.net"]
        current = target
        r = None
        for _ in range(5):
            pu = _up(current)
            if pu.hostname is None or pu.hostname.lower() not in hosts:
                return {"outcome": "upstream_error", "text": None,
                        "diag": "重定向 host 出域（%s）" % (pu.hostname or "无")}
            r = sess.get(current, timeout=20, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location") or ""
                if not loc:
                    break
                current = loc if loc.startswith("http") else (
                    pu.scheme + "://" + pu.netloc + loc)
                continue
            break
        if r is None or r.status_code != 200:
            return {"outcome": "http_error", "text": None,
                    "diag": "HTTP %s" % (getattr(r, "status_code", None) if r else "?")}
        r.encoding = "utf-8"
        body = re.sub(r"<script[\s\S]*?</script>", "", r.text)
        body = re.sub(r"<style[\s\S]*?</style>", "", body)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"(&nbsp;|\s)+", " ", body).strip()
        if len(body) < 50:
            return {"outcome": "parse_error", "text": None,
                    "diag": "详情正文过短（%d 字；签名可能失效，请重新检索后传新链接）"
                            % len(body)}
        return {"outcome": "ok", "text": body[:4000], "diag": ""}
    except Exception as e:
        return {"outcome": "network_error", "text": None, "diag": "网络错误（%s）" % e}


JWW_BASE = "https://jww.zjgsu.edu.cn"


def fetch_jww_notices(page: int = 1):
    """F06：教务处事务通知列表（jww.zjgsu.edu.cn，匿名公开源；单引号 href）。"""
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    path = JWW_BASE + ("/1335/list.htm" if page <= 1
                       else "/1335/list%d.htm" % page)
    # R69：列表抓取逐跳校验（禁自动跟随——防外域跳转）
    try:
        r = sess.get(path, timeout=20, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location") or ""
            from urllib.parse import urlparse as _up
            pu = _up(loc if loc.startswith("http") else JWW_BASE + loc)
            if pu.hostname is None or pu.hostname.lower() != "jww.zjgsu.edu.cn":
                return {"outcome": "upstream_error", "items": None,
                        "diag": "列表重定向 host 出域（%s）" % (pu.hostname or "无")}
            r = sess.get(loc if loc.startswith("http") else JWW_BASE + loc, timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "items": None,
                "diag": "网络错误（%s）" % e}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None,
                "diag": "HTTP %s" % r.status_code}
    r.encoding = "utf-8"
    arts = re.findall(r"href='([^']*c1335a\d+/page\.htm)'[^>]*title='([^']*)'", r.text)
    items = []
    seen = set()
    for u2, t2 in arts:
        t2 = t2.strip()
        if u2 in seen or not t2:
            continue
        seen.add(u2)
        items.append({"title": t2, "url": JWW_BASE + u2, "date": None})
    if not items:
        return {"outcome": "parse_error", "items": None, "diag": "通知条目解析为空"}
    return {"outcome": "ok", "items": items, "diag": ""}


def fetch_jww_notice_detail(url: str):
    """F06：教务处通知正文（匿名 GET）。"""
    return fetch_detail_text(url, max_len=6000,
                             allowed_hosts=["jww.zjgsu.edu.cn"])


def compute_current_week(start_date_text: str, today=None):
    """F06：从教学时间通知解析开学日期并计算当前教学周（R69 收口）。

    仅当文本含精确触发短语「起全校正式上课」时才计算（service 层保证）；
    年份推断沿用「9-12 月开学→当年、1-2 月→上一年」，但**不猜学期适用范围**——
    跨年/春季/历史通知的适用性由调用方按通知标题/正文确认，无依据返回未知。"""
    import datetime as _dt
    # R69：仅精确触发短语才计算（报到日/其它日期不算上课周）
    if "起全校正式上课" not in (start_date_text or ""):
        return {"week": None, "start": None,
                "diag": "未找到精确的「起全校正式上课」表述——周次未知（不猜测）"}
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", start_date_text or "")
    if not m:
        return {"week": None, "start": None, "diag": "日期解析失败"}
    month, day = int(m.group(1)), int(m.group(2))
    today = today or _dt.date.today()
    # 学期跨年（9-12 月开学 → 当年；1-2 月开学 → 上一年）
    year = today.year if month >= 6 else today.year - 1
    try:
        start = _dt.date(year, month, day)
    except ValueError:
        return {"week": None, "start": None, "diag": "日期构造失败"}
    if today < start:
        return {"week": None, "start": start.isoformat(),
                "diag": "学期尚未开始（%s）" % start.isoformat()}
    days = (today - start).days
    week = days // 7 + 1
    return {"week": week, "start": start.isoformat(), "days": days}


TZJK_BASE = "https://tzjk.zjgsu.edu.cn"


def fetch_fitness_scores(token: str, student_no: str):
    """F17：体测成绩（tzjk.zjgsu.edu.cn，Bearer token，本人视角）。

    R70：period 前置校验（HTTP/JSON 根/exam.id——失败不发 mark 依赖请求）；
    mark 根结构受控；success=false/无权限/合法空/未知结构严格分类。"""
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "Authorization": "Bearer " + token,
                         "content-type": "application/json"})
    # ---- period 前置校验（R70：503/错根/缺 id 即停，不发 mark 依赖请求） ----
    try:
        r0 = sess.get(TZJK_BASE + "/exam/api/subscribe/period", timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "data": None, "diag": "网络错误（%s）" % e}
    if r0.status_code in (401, 403):
        return {"outcome": "upstream_error", "data": None,
                "diag": "体测 token 失效（需 App 重新打开体测页后更新）"}
    if r0.status_code != 200:
        return {"outcome": "http_error", "data": None,
                "diag": "period HTTP %s" % r0.status_code}
    try:
        d0 = r0.json()
    except Exception:
        return {"outcome": "parse_error", "data": None, "diag": "period 响应非 JSON"}
    if not isinstance(d0, dict):
        return {"outcome": "parse_error", "data": None, "diag": "period 根非对象"}
    exam = d0.get("exam")
    if not isinstance(exam, dict) or exam.get("id") is None:
        return {"outcome": "upstream_error", "data": None,
                "diag": "period 缺少 exam.id（来源结构异常）"}
    exam_id = exam.get("id")
    # ---- mark 成绩查询 ----
    try:
        r = sess.post(TZJK_BASE + "/exam/api/mark/web/query",
                      data=json.dumps({"exam": exam_id, "studentNo": student_no}),
                      timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "data": None, "diag": "网络错误（%s）" % e}
    if r.status_code == 403:
        return {"outcome": "upstream_error", "data": None,
                "diag": "来源拒绝（403——无权限，原因未知）"}
    if r.status_code != 200:
        return {"outcome": "http_error", "data": None, "diag": "HTTP %s" % r.status_code}
    try:
        d = r.json()
    except Exception:
        return {"outcome": "parse_error", "data": None, "diag": "响应非 JSON"}
    if not isinstance(d, dict):
        return {"outcome": "parse_error", "data": None, "diag": "mark 根非对象"}
    # R70 扩展：success=false 或业务 code=403 → 明确失败（不伪装空记录）
    if d.get("success") is False or d.get("code") == 403 or d.get("code") == "403":
        return {"outcome": "upstream_error", "data": None,
                "diag": "来源业务失败（%s）" % (d.get("message") or d.get("msg")
                                               or d.get("errorMsg") or "无消息")}
    content = d.get("content")
    # 合法空：success=true 且 content 为 null/空 → ok_empty（0 条）
    if content in (None, "", [], {}):
        return {"outcome": "ok_empty", "data": {}, "diag": "无成绩记录（来源空）"}
    if not isinstance(content, dict):
        # 未知结构 → parse_error（不造记录）
        return {"outcome": "parse_error", "data": None,
                "diag": "mark content 结构未知（%s）" % type(content).__name__}
    # R70：本人绑定——返回的 studentNo 与请求的不符 → 拒绝
    ret_no = content.get("studentNo")
    if ret_no and str(ret_no) != str(student_no):
        return {"outcome": "upstream_error", "data": None,
                "diag": "来源返回学号与请求不符（身份绑定校验失败）"}
    return {"outcome": "ok", "data": content, "diag": ""}


XSZKDH_BASE = "https://xszkdh.zjgsu.edu.cn"


XSZKDH_ALLOWED_HOSTS = ("uia.zjgsu.edu.cn", "xszkdh.zjgsu.edu.cn")


def fetch_xszkdh_token(jwweb_cookies):
    """F13：xszkdh 学生智慧导航 token（CAS TGT → type=65 SSO → zbpToken/role）。

    R71：逐跳 host 白名单校验（禁未校验自动跟随）；异常受控。"""
    import requests
    from urllib.parse import urlparse as _up
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    _restore_session_cookies(sess, jwweb_cookies)
    service = "https://xszkdh.zjgsu.edu.cn/sso/login?type=65"
    current = ("https://uia.zjgsu.edu.cn/cas/login?service="
               + requests.utils.quote(service, safe=""))
    r2 = None
    try:
        # R71：逐跳校验（最多 6 跳——每跳 host 白名单）
        for _ in range(6):
            _h, hop_err = _hop_check(current, XSZKDH_ALLOWED_HOSTS)
            if hop_err:
                return {"outcome": "upstream_error", "token": None, "role": None,
                        "diag": hop_err}
            r2 = sess.get(current, timeout=20, allow_redirects=False)
            if r2.status_code in (301, 302, 303, 307, 308):
                loc = r2.headers.get("Location") or ""
                if not loc:
                    break
                # r3 反馈：相对 Location 基于当前 URL 解析（原 pu 引用未定义→NameError）
                _pu = _up(current)
                current = loc if loc.startswith("http") else (
                    _pu.scheme + "://" + _pu.netloc + loc)
                continue
            break
        if r2 is None:
            return {"outcome": "upstream_error", "token": None, "role": None,
                    "diag": "SSO 无响应"}
        if r2.status_code != 200:
            return {"outcome": "upstream_error", "token": None, "role": None,
                    "diag": "SSO HTTP %s" % r2.status_code}
        pairs = dict(re.findall(r"document\.cookie\s*=\s*'([^=]+)=([^;']*);", r2.text))
        token = pairs.get("zbpToken") or pairs.get("token")
        if not token:
            return {"outcome": "upstream_error", "token": None, "role": None,
                    "diag": "SSO 页面未返回 token"}
        return {"outcome": "ok", "token": token, "role": pairs.get("role") or "xs",
                "diag": ""}
    except requests.RequestException as e:
        return {"outcome": "network_error", "token": None, "role": None,
                "diag": _net_err(e)}


def fetch_evaluation_result(token: str, role: str):
    """F13：综合测评结果（zbp-evaluation-mobile，本人视角）。

    周期外（无进行中的测评）来源返回 403 无权限——如实 unavailable。"""
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "zbpToken": token, "zbp-role-no": role})
    base = XSZKDH_BASE + "/evaluation-api/CphsTstEvluResGnrt"
    try:
        r = sess.get(base + "/getResult", timeout=20, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            # B03 r2 反馈③：后续业务请求不自动跳转（逐跳边界未覆盖的出口补齐）
            return {"outcome": "upstream_error", "data": None,
                    "diag": "综测接口返回重定向（%s）——不自动跟随" % r.status_code}
    except Exception as e:
        return {"outcome": "network_error", "data": None,
                "diag": "网络错误（%s）" % type(e).__name__}
    if r.status_code != 200:
        return {"outcome": "http_error", "data": None, "diag": "HTTP %s" % r.status_code}
    try:
        d = r.json()
    except Exception:
        return {"outcome": "parse_error", "data": None, "diag": "响应非 JSON"}
    if d.get("state") == 403:
        # R71：403 = 来源拒绝（原因未知——不推断为"周期外"）
        return {"outcome": "upstream_error", "data": None,
                "diag": "来源拒绝（403——无权限，原因未知/可能无进行中测评）"}
    if d.get("state") != 200:
        return {"outcome": "upstream_error", "data": None,
                "diag": "来源返回异常（state=%s：%s）" % (d.get("state"), d.get("msg"))}
    # R71：data 字典验证（非对象 → parse_error）
    data = d.get("data")
    if data is not None and not isinstance(data, dict):
        return {"outcome": "parse_error", "data": None,
                "diag": "结果 data 结构未知（%s）" % type(data).__name__}
    return {"outcome": "ok", "data": data or {}, "diag": ""}


def fetch_fitness_schedule(token: str):
    """F17 扩展：体测预约时段（period 接口——学期/有效时间/每日时段）。"""
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "Authorization": "Bearer " + token,
                         "content-type": "application/json"})
    try:
        r = sess.get(TZJK_BASE + "/exam/api/subscribe/period", timeout=20)
    except Exception as e:
        return {"outcome": "network_error", "data": None, "diag": "网络错误（%s）" % e}
    if r.status_code in (401, 403):
        return {"outcome": "upstream_error", "data": None,
                "diag": "体测 token 失效（需 App 重新打开体测页）"}
    if r.status_code != 200:
        return {"outcome": "http_error", "data": None, "diag": "HTTP %s" % r.status_code}
    try:
        d = r.json()
    except Exception:
        return {"outcome": "parse_error", "data": None, "diag": "响应非 JSON"}
    # R70：根结构受控（非对象/空根/无 exam → parse_error，不造假记录）
    if not isinstance(d, dict) or not d:
        return {"outcome": "parse_error", "data": None, "diag": "period 根空或非对象"}
    if not isinstance(d.get("exam"), dict) or d["exam"].get("id") is None:
        return {"outcome": "parse_error", "data": None,
                "diag": "period exam 结构异常（缺 id）"}
    return {"outcome": "ok", "data": d, "diag": ""}


def _restore_session_cookies(sess, cookies):
    """B03 反馈 P1：恢复会话 Cookie 保留安全属性（secure/expires），不再只取 name/value。

    过期 secure Cookie 被随 HTTP 请求发送的复现路径即来自属性丢失；缺失 domain/name
    的条目跳过（不静默迁移）。"""
    for c in (cookies or []):
        try:
            if not c.get("name") or not c.get("domain") or "value" not in c:
                continue
            sess.cookies.set(c["name"], c["value"], domain=c["domain"],
                             path=c.get("path") or "/",
                             secure=bool(c.get("secure")),
                             # r3 反馈：expires=0 是"已到期"值，不得 or None 变会话 Cookie
                             expires=(None if c.get("expires") is None
                                      else int(c.get("expires"))))
        except Exception:
            continue
    return sess


def _hop_check(url: str, allowed_hosts, https_only: bool = True):
    """B03 反馈 P1+r2：逐跳校验 scheme+host+端口（https 仅 443——8443 等非预期端口拒绝）。

    返回 (host, err)。"""
    from urllib.parse import urlparse as _up
    try:
        pu = _up(url)
        port = pu.port  # :invalid 等非法端口在访问时抛 ValueError——受控
    except ValueError:
        return None, "重定向 URL 非法（端口/格式无效）"
    host = (pu.hostname or "").lower()
    if host not in allowed_hosts:
        return None, "重定向 host 出域（%s）" % (pu.hostname or "无")
    if https_only and pu.scheme != "https":
        return None, "重定向降级为非 HTTPS（%s）" % pu.scheme
    if https_only and port not in (None, 443):
        return None, "重定向端口非预期（%s:%s）" % (host, port)
    return host, None


def _net_err(e):
    """B03 反馈 P1：网络异常只回类型名——RequestException 文本可能含含 ticket 的完整 URL。"""
    return "网络错误（%s）" % type(e).__name__


SJJX_BASE = "https://sjjx.zjgsu.edu.cn"
SJJX_CLIENT = "https://sjjx.zjgsu.edu.cn/teachApp/pages/caslogin"


def fetch_labor_courses(jwweb_cookies):
    """F16：劳动教育课程中心项目列表（sjjx teachApp，本人视角）。

    R71 同款纪律：逐跳 host 白名单（uia/sjjx）；appCas/login 需 ticket+callback
    两参数（项目使用者实测 ✓）；Bearer token 后续调用。"""
    import requests
    from urllib.parse import urlparse as _up, quote as _quote
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    _restore_session_cookies(sess, jwweb_cookies)
    allowed = ("uia.zjgsu.edu.cn", "sjjx.zjgsu.edu.cn")
    try:
        # 1) CAS ticket（逐跳白名单+HTTPS 强制）
        current = ("https://uia.zjgsu.edu.cn/cas/login?service="
                   + _quote(SJJX_CLIENT, safe=""))
        ticket = None
        for _ in range(6):
            _h, hop_err = _hop_check(current, allowed)
            if hop_err:
                return {"outcome": "upstream_error", "items": None, "diag": hop_err}
            rr = sess.get(current, timeout=20, allow_redirects=False)
            if rr.status_code in (301, 302, 303, 307, 308):
                loc = rr.headers.get("Location") or ""
                if not loc:
                    break
                _pu = _up(current)
                current = loc if loc.startswith("http") else (
                    _pu.scheme + "://" + _pu.netloc + loc)
                m = re.search(r'ticket=([^&]+)', current)
                if m:
                    ticket = m.group(1)
                continue
            break
        if not ticket:
            return {"outcome": "upstream_error", "items": None,
                    "diag": "CAS 未返回 ticket（TGT 失效或风控）"}
        # 2) appCas/login（ticket+callback → token）
        r3 = sess.get(SJJX_BASE + "/teach-api/appCas/login",
                      params={"ticket": ticket, "callback": SJJX_CLIENT}, timeout=20,
                      allow_redirects=False)
        d3 = r3.json() if r3.text.strip().startswith("{") else {}
        token = d3.get("token")
        if r3.status_code in (301, 302, 303, 307, 308):
            return {"outcome": "upstream_error", "items": None,
                    "diag": "appCas/login 返回重定向（%s）——不自动跟随" % r3.status_code}
        if r3.status_code != 200 or not token:
            return {"outcome": "upstream_error", "items": None,
                    "diag": "appCas/login 失败（%s）" % (d3.get("msg") or r3.status_code)}
        # 3) 项目列表（Bearer）
        sess.headers.update({"Authorization": "Bearer " + token})
        r4 = sess.get(SJJX_BASE + "/teach-api/platform/projectInfo/getAppProjectList",
                      timeout=20, allow_redirects=False)
        if r4.status_code != 200:
            return {"outcome": "http_error", "items": None,
                    "diag": "项目列表 HTTP %s" % r4.status_code}
        d4 = r4.json()
        # R72：业务码/根/rows 逐层校验（HTTP200+code 非零 → 失败；rows 非 list → parse_error）
        if d4.get("code") not in (200, "200", None):
            return {"outcome": "upstream_error", "items": None,
                    "diag": "来源业务失败（code=%s：%s）" % (d4.get("code"), d4.get("msg"))}
        rows = d4.get("rows")
        if rows is not None and not isinstance(rows, list):
            return {"outcome": "parse_error", "items": None,
                    "diag": "rows 结构未知（%s）" % type(rows).__name__}
        return {"outcome": "ok", "items": rows or [], "total": d4.get("total"),
                "diag": ""}
    except requests.RequestException as e:
        return {"outcome": "network_error", "items": None, "diag": _net_err(e)}


def fetch_labor_course_detail(jwweb_cookies, project_id: str):
    """B03：劳动课程详情+本人记录（详情+getMyCourseInfo——同一 token 链）。

    详情/本人记录对未报名或无单元的项目返回"未查询到"——如实 ok_empty 分类。"""
    import requests
    from urllib.parse import urlparse as _up, quote as _quote
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    _restore_session_cookies(sess, jwweb_cookies)
    allowed = ("uia.zjgsu.edu.cn", "sjjx.zjgsu.edu.cn")
    try:
        current = ("https://uia.zjgsu.edu.cn/cas/login?service="
                   + _quote(SJJX_CLIENT, safe=""))
        ticket = None
        for _ in range(6):
            _h, hop_err = _hop_check(current, allowed)
            if hop_err:
                return {"outcome": "upstream_error", "detail": None, "mine": None,
                        "diag": hop_err}
            rr = sess.get(current, timeout=20, allow_redirects=False)
            if rr.status_code in (301, 302, 303, 307, 308):
                loc = rr.headers.get("Location") or ""
                if not loc:
                    break
                _pu = _up(current)
                current = loc if loc.startswith("http") else (
                    _pu.scheme + "://" + _pu.netloc + loc)
                m = re.search(r'ticket=([^&]+)', current)
                if m:
                    ticket = m.group(1)
                continue
            break
        if not ticket:
            return {"outcome": "upstream_error", "detail": None, "mine": None,
                    "diag": "CAS 未返回 ticket（TGT 失效或风控）"}
        r3 = sess.get(SJJX_BASE + "/teach-api/appCas/login",
                      params={"ticket": ticket, "callback": SJJX_CLIENT}, timeout=20,
                      allow_redirects=False)
        d3 = r3.json() if r3.text.strip().startswith("{") else {}
        token = d3.get("token")
        if r3.status_code in (301, 302, 303, 307, 308):
            return {"outcome": "upstream_error", "detail": None, "mine": None,
                    "diag": "appCas/login 返回重定向（%s）——不自动跟随" % r3.status_code}
        if r3.status_code != 200 or not token:
            return {"outcome": "upstream_error", "detail": None, "mine": None,
                    "diag": "appCas/login 失败（%s）" % (d3.get("msg") or r3.status_code)}
        sess.headers.update({"Authorization": "Bearer " + token})

        def _read_one(url, what):
            """B03 反馈 P2：单接口读取严格分类——HTTP/根/业务码/data 逐层校验，
            失败/未知结构一律受控 outcome（不再整 ok 造全 null 记录）。"""
            rr = sess.get(url, timeout=20, allow_redirects=False)
            if rr.status_code in (301, 302, 303, 307, 308):
                return {"outcome": "upstream_error",
                        "diag": "%s 返回重定向（%s）——不自动跟随" % (what, rr.status_code)}
            if rr.status_code != 200:
                return {"outcome": "http_error",
                        "diag": "%s HTTP %s" % (what, rr.status_code)}
            if not rr.text.strip().startswith("{"):
                return {"outcome": "parse_error", "diag": "%s 响应非 JSON" % what}
            try:
                dd = rr.json()
            except Exception:
                return {"outcome": "parse_error", "diag": "%s JSON 解析失败" % what}
            if not isinstance(dd, dict):
                return {"outcome": "parse_error", "diag": "%s 根非对象" % what}
            code = dd.get("code")
            if code is not None and code not in (200, "200"):
                return {"outcome": "upstream_error",
                        "diag": "%s 来源业务失败（code=%s：%s）" % (what, code, dd.get("msg"))}
            data = dd.get("data")
            if data is None or data == {}:
                # 来源明确"未查询到"/空 data → 合法空（note 记来源说明）
                return {"outcome": "ok_empty", "data": None,
                        "note": dd.get("msg") or "未查询到"}
            if not isinstance(data, dict):
                return {"outcome": "parse_error",
                        "diag": "%s data 结构未知（%s）" % (what, type(data).__name__)}
            return {"outcome": "ok", "data": data, "note": ""}

        rd = _read_one(SJJX_BASE + "/teach-api/platform/projectInfo/"
                       + "getAppProjectCourseDetail/" + project_id, "详情")
        if rd["outcome"] not in ("ok", "ok_empty"):
            return {"outcome": rd["outcome"], "detail": None, "mine": None,
                    "diag": rd.get("diag") or ""}
        rm = _read_one(SJJX_BASE + "/teach-api/platform/projectApply/"
                       + "getMyCourseInfo/" + project_id, "本人记录")
        if rm["outcome"] not in ("ok", "ok_empty"):
            return {"outcome": rm["outcome"], "detail": None, "mine": None,
                    "diag": rm.get("diag") or ""}
        detail = rd.get("data")
        mine = rm.get("data")
        detail_note = rd.get("note") or ""
        mine_note = rm.get("note") or ""
        outcome = "ok" if (detail or mine) else "ok_empty"
        return {"outcome": outcome, "detail": detail, "mine": mine,
                "detail_note": detail_note, "mine_note": mine_note, "diag": ""}
    except requests.RequestException as e:
        return {"outcome": "network_error", "detail": None, "mine": None,
                "diag": _net_err(e)}


# 学生手册（B03-③）：默认 2026 最新版（学生处 xsc 2026-09-20 发布）；2021 旧版备选
STUDENT_HANDBOOK_URL = ("http://xsc.zjgsu.edu.cn/_upload/article/files/59/9f/"
                        "e779f5284c3683c973b729600516/"
                        "82f396fc-8a87-4fce-b158-65d1948ef6b1.pdf")
STUDENT_HANDBOOK_HOST = "xsc.zjgsu.edu.cn"
STUDENT_HANDBOOK_VERSION = "2026"
STUDENT_HANDBOOK_URL_2021 = ("https://econet.zjgsu.edu.cn/_upload/article/files/35/"
                             "0e/283564cc441a8f2d4410de07eec4/"
                             "308dde4f-3d13-4db0-9ff4-e0e4250d64b7.pdf")


def fetch_student_handbook(keyword: str = ""):
    """B03-③：学生手册正文（2021 汇编版 PDF，econet 公开源，匿名）。

    keyword 按章节标题/正文匹配（返回匹配页码及上下文）；无 keyword 返回目录。"""
    import io as _io
    import requests
    try:
        r = requests.get(STUDENT_HANDBOOK_URL, timeout=90)
    except Exception as e:
        return {"outcome": "network_error", "toc": None, "sections": None,
                "pages": None, "diag": "网络错误（%s）" % e}
    if r.status_code != 200:
        return {"outcome": "http_error", "toc": None, "sections": None,
                "pages": None, "diag": "HTTP %s" % r.status_code}
    try:
        from pypdf import PdfReader
        reader = PdfReader(_io.BytesIO(r.content))
    except Exception as e:
        return {"outcome": "parse_error", "toc": None, "sections": None,
                "pages": None, "diag": "PDF 解析失败（%s）" % e}
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    # 目录（B03 反馈 P2：实测 2026 版目录在第 1-2 页——pages[0:2]；旧 [1:4] 漏首页混正文）
    toc = "\n".join(pages[0:2])
    if not keyword or not keyword.strip():
        return {"outcome": "ok", "toc": toc, "sections": None,
                "pages": len(pages), "diag": ""}
    # keyword 检索（标题/正文匹配——返回命中页上下文）
    kw = keyword.strip()
    sections = []
    for i, text in enumerate(pages):
        if kw in text:
            j = text.find(kw)
            ctx = text[max(0, j - 150): j + 700]
            sections.append({"page": i + 1, "context": ctx.strip()})
            if len(sections) >= 5:
                break
    truncated = len(sections) >= 5
    return {"outcome": "ok", "toc": None, "sections": sections,
            "pages": len(pages),
            "diag": ("无匹配章节（%s）" % kw if not sections else
                     ("命中截断：仅返回前 5 处（%s）" % kw if truncated else ""))}


# ---------------- F14：学生处公开公示（评奖评优/资助评审公示，匿名公开源） ----------------

import urllib.parse

UA = "Mozilla/5.0"
XSC_BASE = "https://xsc.zjgsu.edu.cn"
XSC_PUB_LIST_PATH = "/1827/list.htm"       # 第一页；翻页为 list2.htm/list3.htm...
XSC_PUB_HOST = "xsc.zjgsu.edu.cn"


def _xsc_publication_entries(html: str):
    """解析 /1827 列表页：条目（标题/链接/日期）。

    页面结构（2026-10-16 实测）：wp_article_list 内
    <a href="/2026/1015/c1827a232808/page.htm" title="标题">标题</a> + 相邻日期文本。"""
    items = []
    seen = set()
    # r3 反馈③：限定实际文章列表区域（news_list <ul>），nav 中的外域仿公示链接不再混入
    regions = re.findall(r'<ul[^>]+class=["\'][^"\']*news_list[^"\']*["\'][^>]*>([\s\S]*?)</ul>',
                          html)
    # r4 反馈：来源模板缺失（无 news_list）→ 结构未证，不整页回退
    # （导航内同源历史 c1827a 链接不得补足为"当前公示"）
    if not regions:
        return None
    scan_html = "".join(regions)
    # 页面实测（2026-10-16）：href/title 单引号，条目结构
    # <span class="news_title"><a href='...' title='...'>标题</a></span>
    # <span class="news_meta">yyyy-MM-dd</span></li>
    for m in re.finditer(
            r"<a[^>]+href=[\"']([^\"']*c1827a\d+/[^\"']*\.htm)[\"'][^>]*>([\s\S]{4,160}?)</a>"
            r"((?:(?!<a[ \s>])[\s\S]){0,300}?)</li>",
            scan_html):
        url, body, tail = m.group(1), m.group(2), m.group(3)
        title = re.sub(r"<[^>]+>", "", body).strip()
        if not title or url in seen:
            continue
        full = urllib.parse.urljoin(XSC_BASE + "/", url)
        # r3 反馈③：核验返回来源域（仅 xsc.zjgsu.edu.cn）
        if urllib.parse.urlparse(full).hostname != XSC_PUB_HOST:
            continue
        seen.add(url)
        dm = re.search(r"(20\d{2}-\d{2}-\d{2})", tail)
        items.append({"title": title, "url": full,
                      "date": dm.group(1) if dm else None})
    return items


def fetch_scholarship_publications(keyword: str = "", page: int = 1, detail_url: str = ""):
    """F14：学生处公开公示栏（评奖评优/资助评审公示）。

    - detail_url 非空：返回该公示正文原文（≤6000 字，附件链接列表）；
    - 否则：列表第 page 页（keyword 为标题子串过滤，当前页内）。
    公开匿名源；获奖名单以公示原文/纸质文件为准。"""
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    if detail_url:
        pu = urllib.parse.urlparse(detail_url)
        if pu.hostname != XSC_PUB_HOST:
            return {"outcome": "upstream_error", "items": None, "detail": None,
                    "attachments": None, "diag": "detail_url 出域（仅 xsc.zjgsu.edu.cn）"}
        try:
            r = sess.get(detail_url, timeout=20)
        except requests.RequestException as e:
            return {"outcome": "network_error", "items": None, "detail": None,
                    "attachments": None, "diag": "网络错误（%s）" % e}
        if r.status_code != 200:
            return {"outcome": "http_error", "items": None, "detail": None,
                    "attachments": None, "diag": "HTTP %s" % r.status_code}
        r.encoding = "utf-8"
        # B03 r2 反馈②：正文限定 wp_article_content 区（导航/业务错误提示不再当正文入库）
        page_html = re.sub(r"<script[\s\S]*?</script>", "", r.text)
        page_html = re.sub(r"<style[\s\S]*?</style>", "", page_html)
        # r3 反馈④：class token 精确匹配（拒绝 not_wp_article_content_suffix 类子串）
        body_el = None
        for cm in re.finditer(r'<div\b[^>]*class=["\']([^"\']*)["\'][^>]*>', page_html):
            cls = cm.group(1)
            if re.search(r'(?:^|\s)wp_article_?content(?:\s|$)', cls):
                body_el = cm
                break
        if body_el is None:
            return {"outcome": "parse_error", "items": None, "detail": None,
                    "attachments": None, "diag": "未找到正文区（wp_articlecontent）——不入库"}
        # 从所属 <div 元素起点做深度计数，截到该元素闭合——闭合后的 nav/附件不再混入
        div_start = body_el.start()  # match 即 <div 起点（rfind 在文档首元素时返回 -1）
        depth = 0
        end_i = len(page_html)
        for tag in re.finditer(r'<div\b|</div>', page_html[div_start:]):
            if tag.group(0) == "<div":
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    end_i = div_start + tag.end()
                    break
        body = page_html[div_start:end_i]

        def _clean_url(base, href):
            """urljoin + 净化：去 userinfo/fragment，仅 http(s)。非法返回 None。"""
            u = urllib.parse.urljoin(base, (href or "").strip())
            pu = urllib.parse.urlparse(u)
            if pu.scheme not in ("http", "https") or not pu.hostname:
                return None
            netloc = pu.hostname + (":%d" % pu.port if pu.port else "")
            return urllib.parse.urlunparse((pu.scheme, netloc,
                                            pu.path or "/", pu.params,
                                            pu.query, ""))

        # 附件链接（正文区内；引号兼容；urljoin 处理相对地址）
        attachments = []
        for m in re.finditer(r'<a[^>]+href=[\"\']([^\"\']*(?:/_upload/article/files/[^\"\']*|\.(?:pdf|docx?|xlsx?|zip|rar))(?:[?#][^\"\']*)?)[\"\'][^>]*>([\s\S]{0,160}?)</a>',
                             body, re.I):
            u2 = m.group(1)
            t2 = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            cu = _clean_url(detail_url, u2)
            if not cu:
                continue
            attachments.append({"url": cu, "title": t2 or "附件"})
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"(&nbsp;|\s)+", " ", text).strip()
        if len(text) < 20:
            return {"outcome": "parse_error", "items": None, "detail": None,
                    "attachments": None, "diag": "正文区文本过短（结构可能变化）——不入库"}
        return {"outcome": "ok", "items": None, "detail": text[:6000],
                "attachments": attachments[:10], "diag": ""}
    if not isinstance(page, int) or page < 1 or page > 50:
        return {"outcome": "upstream_error", "items": None, "detail": None,
                "attachments": None, "diag": "page 须为 1..50"}
    list_url = (XSC_BASE + XSC_PUB_LIST_PATH) if page == 1 else (
        XSC_BASE + "/1827/list%d.htm" % page)
    try:
        r = sess.get(list_url, timeout=20)
    except requests.RequestException as e:
        return {"outcome": "network_error", "items": None, "detail": None,
                "attachments": None, "diag": "网络错误（%s）" % e}
    if r.status_code == 404 and page > 1:
        # 翻页超界（来源翻到无页）→ 合法空
        return {"outcome": "ok_empty", "items": [], "detail": None,
                "attachments": None, "diag": "翻页超界（list%d.htm 不存在）" % page}
    if r.status_code != 200:
        return {"outcome": "http_error", "items": None, "detail": None,
                "attachments": None, "diag": "HTTP %s" % r.status_code}
    r.encoding = "utf-8"
    entries = _xsc_publication_entries(r.text)
    if entries is None:
        # r4 反馈：无列表区=来源模板缺失——parse_error 受控（服务层保旧），不整页补足
        return {"outcome": "parse_error", "items": None, "detail": None,
                "attachments": None, "diag": "页面无公示列表区（news_list 缺失）——不入库"}
    items = entries
    if not items:
        # 第一页有列表区但 0 条 → 结构变化（不是"无公示"）
        return {"outcome": "parse_error", "items": None, "detail": None,
                "attachments": None, "diag": "列表页未解析出条目（结构可能变化）"}
    kw = (keyword or "").strip()
    if kw:
        items = [it for it in items if kw in it["title"]]
    return {"outcome": ("ok_empty" if not items else "ok"), "items": items,
            "detail": None, "attachments": None, "diag": ""}


# ---------------- F26：图书馆数字资源导航（公开静态目录） ----------------

LIB_HTTP = "http://lib.zjgsu.edu.cn"
# 数字资源子栏目（2026-10-16 实测 /1013/list.htm 左侧导航）
DIGITAL_CATEGORIES = {
    "all": ("全部数字资源", "/1013/list.htm"),
    "chinese_journal": ("中文电子期刊", "/1020/list.htm"),
    "foreign_journal": ("外文电子期刊", "/1021/list.htm"),
    "ebook": ("电子图书", "/1022/list.htm"),
    "thesis": ("学位、会议论文", "/1023/list.htm"),
    "econ_data": ("经济资讯、数据", "/1024/list.htm"),
    "multimedia": ("多媒体学习资源", "/1025/list.htm"),
    "others": ("其他数据库", "/1027/list.htm"),
    "trial": ("试用数据库", "/1028/list.htm"),
}
def _lib_private_host(url: str) -> bool:
    """内网地址判定（RFC1918；仅用于 access_way 说明，不改变 URL 本身）。"""
    m = re.match(r"https?://([^/:]+)", url or "")
    if not m:
        return False
    host = m.group(1)
    if re.match(r"^10\.\d+\.\d+\.\d+$", host):
        return True
    if re.match(r"^192\.168\.\d+\.\d+$", host):
        return True
    if re.match(r"^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$", host):
        return True
    return False


def fetch_digital_resources(category: str = "all", keyword: str = ""):
    """F26：图书馆数字资源导航（资源名/入口 URL/简介链接/访问方式说明）。

    B03 r2 反馈语义（2026-10-16）：
    - keyword 无命中（各栏目解析正常但过滤后为空）→ ok_empty（不是来源失败）；
    - HTTP200 但无资源正文区/解析 0 条 → parse_error 计入 failed（结构未证≠合法空）；
    - 聚合失败栏目具名登记（failed_categories），交由服务层转 partial 保旧；
    - access_way 为说明性推断：内网 IP（10.x 等）→「校内网/VPN」；其余不猜 CARSI 覆盖。
    公开静态目录；匿名可读。"""
    import requests
    meta = DIGITAL_CATEGORIES.get(category)
    if not meta:
        return {"outcome": "upstream_error", "items": None,
                "diag": "未知 category（%s）" % category}
    cats = (list(DIGITAL_CATEGORIES.items()) if category == "all"
            else [(category, meta)])
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    items = []
    failed = []        # 失败/结构未证栏目（具名）
    raw_total = 0      # keyword 过滤前的原始条目数
    kw = (keyword or "").strip()
    try:
        for code, (label, path) in cats:
            try:
                r = sess.get(LIB_HTTP + path, timeout=20)
            except requests.RequestException as e:
                if category == "all":
                    failed.append(label)
                    continue
                return {"outcome": "network_error", "items": None,
                        "diag": "网络错误（%s）" % type(e).__name__}
            if r.status_code != 200:
                if category == "all":
                    failed.append(label)
                    continue
                return {"outcome": "http_error", "items": None,
                        "diag": "HTTP %s" % r.status_code}
            r.encoding = "utf-8"
            html = r.text
            # 主内容区（2026-10-16 实测 1020 页）：div ...list-content" 端口的 <ul class="list">
            seg = re.search(r'list-content[\s\S]*?<ul[^>]*>([\s\S]*?)</ul>', html)
            if not seg:
                # HTTP200 但无资源正文区——结构未证，不得当合法空（B03 r2 反馈）
                if category == "all":
                    failed.append(label)
                    continue
                return {"outcome": "parse_error", "items": None,
                        "diag": "栏目页无资源正文区（结构可能变化）"}
            region = seg.group(1)
            raw_before = len(items)
            for m in re.finditer(
                    r'<li>\s*<a\s+href="([^"]+)"\s+title="([^"]*)"[^>]*>\s*([^<]{2,120}?)\s*</a>'
                    r'(?:[\s\S]{0,120}?<a\s+href="([^"]+)"[^>]*>[^<]*资源简介[^<]*</a>)?\s*</li>',
                    region):
                u2, t_attr, t_body, intro = m.group(1), m.group(2), m.group(3), m.group(4)
                name = (t_attr or t_body or "").strip()
                if not name:
                    continue
                raw_total += 1
                if kw and kw not in name:
                    continue
                full = u2 if u2.startswith("http") else LIB_HTTP + u2
                intro_full = None
                if intro:
                    intro_full = intro if intro.startswith("http") else LIB_HTTP + intro
                items.append({"name": name, "category": label,
                              "entry_url": full, "intro_url": intro_full,
                              "access_way": ("校内网/VPN（内网地址）"
                                             if _lib_private_host(full)
                                             else "按入口访问（校外可用性以图书馆说明为准）")})
            _ = raw_before  # （保留原始计数语义：raw_total 为过滤前总数）
        if category == "all" and not failed and raw_total == 0:
            return {"outcome": "upstream_error", "items": None,
                    "diag": "聚合模式未取得任何条目（来源结构变化或网络失败）"}
        # 关键语义：有原始条目但 keyword 过滤后为空 → 合法空（不是来源失败）
        outcome = "ok_empty" if (raw_total > 0 and not items) else (
            "ok" if items else "upstream_error")
        if outcome == "upstream_error":
            return {"outcome": outcome, "items": None,
                    "diag": "解析 0 条（结构可能变化）" if not failed else
                            "全部栏目失败：%s" % "、".join(failed)}
        return {"outcome": outcome, "items": items,
                "failed_categories": failed, "raw_total": raw_total,
                "diag": ("聚合部分栏目失败：%s" % "、".join(failed) if failed else "")}
    except requests.RequestException as e:
        return {"outcome": "network_error", "items": None,
                "diag": "网络错误（%s）" % type(e).__name__}
