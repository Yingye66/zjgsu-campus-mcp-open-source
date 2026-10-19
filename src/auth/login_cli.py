# -*- coding: utf-8 -*-
"""本机登录初始化 CLI（认证初始化与 MCP 请求协议分离）。

用法（在项目根目录、交互式终端中运行）：
    .venv\\Scripts\\python.exe -m src.auth.login_cli init     # 登录并保存本机会话（默认）
    .venv\\Scripts\\python.exe -m src.auth.login_cli status   # 查看会话状态（不含秘密）
    .venv\\Scripts\\python.exe -m src.auth.login_cli logout   # 清除本机会话（缓存随之不可读）

边界（对应探针 P01/P05 与交接文档第 7 节）：
- 凭据仅在本人交互式终端输入（非 TTY 拒绝）；密码不回显、不落盘、不写日志；
- 本 CLI 与 MCP 服务分离：MCP 工具永远不会询问或接收凭据；
- 会话文件（含 zjgsuToken）写入 data/sessions/（私有目录，.gitignore 排除）；
- 输出不包含 token、用户名原文（仅显示不可逆的账号域标识）。
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from ..adapters.campus import auth as campus_auth
from ..adapters.campus.constants import CampusConfigError, load_client_constants
from ..adapters.campus.des import find_java
from ..adapters.campus.transport import UrllibTransport
from ..paths import default_data_dir
from .session import (account_scope_id, clear_active, delete_session, get_active,
                      load_session, save_session, set_active)

TZ = timezone(timedelta(hours=8))

# 独立凭据（不随统一认证会话轮换）：重跑 init 时保留，避免静默丢失
_PRESERVED_SESSION_KEYS = ("seat_cookie", "seat_cookie_saved_at",
                           "tzjk_token", "tzjk_student_no")


def _preserve_independent_credentials(session: dict, old: dict | None) -> list:
    """把旧会话中的独立凭据并入新会话，返回保留的键名（不打印值）。"""
    kept = []
    if not isinstance(old, dict):
        return kept
    for key in _PRESERVED_SESSION_KEYS:
        if old.get(key) and not session.get(key):
            session[key] = old[key]
            kept.append(key)
    return kept


def _now_s() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _print(msg: str = "") -> None:
    print(msg)


def cmd_status() -> int:
    data_dir = default_data_dir()
    acct = get_active(data_dir)
    session = load_session(data_dir, acct) if acct else None
    _print("== 「我的商大」本机会话状态 ==")
    if not acct:
        _print("当前账号域：未设置（请先运行 init）")
        return 1
    _print("账号域标识：%s（不可逆；不含用户名原文）" % acct)
    if not session:
        _print("会话：缺失（请先运行 init）")
        return 1
    _print("会话：存在")
    _print("  用户类型：%s（2=学生）" % session.get("user_type"))
    _print("  建立时间：%s" % session.get("created_at"))
    return 0


def cmd_logout() -> int:
    data_dir = default_data_dir()
    acct = get_active(data_dir)
    if acct:
        delete_session(data_dir, acct)
        clear_active(data_dir)
        _print("本机会话已清除（账号域 %s）。" % acct)
    else:
        _print("本来就没有生效会话。")
    _print("说明：缓存数据行仍在本机 SQLite 中，但在无会话状态下不可通过工具读取；重新登录后恢复。")
    return 0


def cmd_init() -> int:
    data_dir = default_data_dir()

    _print("== 「我的商大」登录初始化（本机） ==")
    _print("凭据仅用于本机向学校接口发起登录；密码不回显、不落盘。")
    _print()

    # —— 工具前置检查（在询问凭据之前） ——
    try:
        constants = load_client_constants(data_dir)
    except CampusConfigError as e:
        _print("已停止（配置缺失）：%s" % e)
        return 2
    java_bin = find_java()
    if not java_bin:
        _print("已停止：未找到可用 java（DES 加密桥需要完整 JDK）。")
        return 2
    _print("工具检查通过：客户端常量 ✓（值不显示）、Java ✓")

    # —— 交互式终端门槛（凭据不进管道/日志） ——
    if not sys.stdin.isatty():
        _print("检测到当前输入不是交互式终端（非 TTY）。为防止凭据进入管道或日志，已停止。")
        _print("请在本人电脑的终端窗口中运行本命令。")
        return 2

    _print()
    try:
        username = input("用户名（学号/工号）：").strip()
        password = getpass.getpass("密码（不回显）：")
    except (KeyboardInterrupt, EOFError):
        _print("已取消输入。")
        return 1
    if not username or not password:
        _print("用户名或密码为空，已退出。")
        return 1

    acct = account_scope_id(username, constants["client_id"])
    transport = UrllibTransport()
    result = campus_auth.perform_login(constants, transport, username, password, java_bin)

    _print()
    if result["outcome"] != "ok":
        # R25：密码引用统一在所有认证路径结束后清除；此处直接退出由局部变量回收
        _print("登录未完成：%s：%s" % (result["outcome"], result["diag"]))
        _print("（未保存任何会话；如需重试请重新运行 init。）")
        return 1

    session = {
        "schema": 1,
        "account_scope_id": acct,
        "client_id": constants["client_id"],
        "zjgsu_token": result["token"],
        "user_type": result["user_type"],
        "created_at": _now_s(),
    }

    # —— 教务网页版（jwglxt）CAS 会话：同一凭据顺带建立（R25：失败/异常都不阻断已成功的 App 会话） ——
    from ..adapters.campus.jwweb import cas_login as jwweb_cas_login
    jw_ok = False
    try:
        jw = jwweb_cas_login(username, password)
        if jw["outcome"] == "ok":
            session["jwglxt_cookies"] = jw["cookies"]
            jw_ok = True
            # §2.78：App 域（my.zjgsu）cookie 顺带保存——服务目录长期入口
            if jw.get("app_cookies"):
                session["app_cookies"] = jw["app_cookies"]
            _print("教务网页版会话 ✓（jwglxt_cookies 已入会话）")
        else:
            _print("教务网页版会话未建立（%s：%s）；"
                   "get_exams/get_grades/get_timetable_web 将不可用，"
                   "可稍后重跑本命令刷新。" % (jw["outcome"], jw["diag"]))
    except Exception as e:
        _print("教务网页版会话异常（%s：%s）；App 会话不受影响，"
               "可稍后重跑本命令刷新。" % (type(e).__name__, e))
    finally:
        # R25：两次认证全部结束后才丢弃明文密码引用
        del password

    # 独立凭据（选座 Cookie / 体测令牌）不属于统一认证会话，重跑 init 不得清掉
    old_session = load_session(data_dir, acct)
    kept = _preserve_independent_credentials(session, old_session)
    save_session(data_dir, acct, session)
    set_active(data_dir, acct)

    _print("登录成功：%s" % result["diag"])
    if kept:
        _print("已保留本机独立凭据：%s（未重新获取）" % "、".join(kept))
    # 身份元数据变化提示（旧会话存在且 user_type 不同）
    old_type = (old_session or {}).get("user_type")
    if old_type is not None and str(old_type) != str(result["user_type"]):
        _print("注意：身份类型发生变化（%s → %s）；涉身份口径的工具输出以新会话为准。"
               % (old_type, result["user_type"]))
    _print("会话已保存（私有目录）；账号域标识：%s" % acct)
    if str(result["user_type"]) != "2":
        _print("注意：当前身份不是学生（user_type=%s），课表模块将显示为不可用。" % result["user_type"])
    _print("下一步：启动 MCP 服务（run_server.ps1）并在客户端中调用 get_timetable。")
    return 0


def cmd_fitness_token() -> int:
    """R70：体测专用令牌导入（临时性质——从 App 体测页 URL 复制 token+学号）。

    用法：python -m src.auth.login_cli fitness-token
    交互式输入 token 与学号（getpass 无回显）；只写入本机会话文件，不打印令牌本身。
    R70 残余修复：数据目录共用 ZJGSU_CAMPUS_DATA_DIR；非 TTY 拒绝；学号脱敏输出。"""
    import getpass
    import sys as _sys
    from pathlib import Path
    import src.auth.session as _S
    # R70 残余：数据目录与既有 paths 配置共用（不硬编码）
    data_dir = (Path(os.environ.get("ZJGSU_CAMPUS_DATA_DIR"))
                if os.environ.get("ZJGSU_CAMPUS_DATA_DIR")
                else Path(__file__).resolve().parents[2] / "data")
    # R70 残余：非 TTY 拒绝（交互式输入不可用时不静默成功）
    if not _sys.stdin.isatty():
        _print("fitness-token 需要交互式输入（当前非终端环境）——请在真实终端运行")
        return 1
    acct = _S.get_active(data_dir)
    if not acct:
        _print("本地无活动账号：请先运行 init")
        return 1
    _print("请先在手机 App 打开一次「体测」页面（成绩查询入口），")
    _print("从其加载的 URL 中复制 token= 之后的一段（到 & 为止）。")
    token = getpass.getpass("token（输入无回显）: ").strip()
    student_no = input("学号: ").strip()
    if not token or not student_no.isdigit():
        _print("token 或学号格式无效（学号须为数字）")
        return 1
    sess = _S.load_session(data_dir, acct) or {}
    old_token = sess.get("tzjk_token")
    old_no = sess.get("tzjk_student_no")
    if bool(old_token) != bool(old_no):
        _print("提示：旧配置中 token/学号不成对（残缺状态），本次将整体替换。")
    sess["tzjk_token"] = token
    sess["tzjk_student_no"] = student_no
    _S.save_session(data_dir, acct, sess)
    # R70 残余：学号脱敏输出（不打印完整学号）
    _print("体测令牌已写入本机会话（学号 %s****%s）。" % (student_no[:4], student_no[-2:]))
    _print("令牌不打印、不进日志；失效后请重新执行本命令。")
    return 0


def cmd_seat_cookie() -> int:
    """R70 模式：选座系统（我去图书馆白标）会话 Cookie 导入。

    背景：选座系统仅支持微信 OAuth（PC 统一认证链路在线取证不可用，2026-10-18），
    会话 Cookie 需手机抓包获取（工具：iOS Stream / Android HttpCanary，
    过滤 wechatseat.zjgsu.edu.cn 的任意请求，复制请求头 Cookie 的完整值）。
    交互式粘贴（getpass 无回显）；只写本机会话文件，不打印、不进日志。
    Cookie 失效（会话过期）后重新抓包并重跑本命令。"""
    import getpass
    import sys as _sys
    import src.auth.session as _S
    from pathlib import Path
    data_dir = (Path(os.environ.get("ZJGSU_CAMPUS_DATA_DIR"))
                if os.environ.get("ZJGSU_CAMPUS_DATA_DIR")
                else Path(__file__).resolve().parents[2] / "data")
    if not _sys.stdin.isatty():
        _print("seat-cookie 需要交互式粘贴（当前非终端环境）——请在真实终端运行")
        return 1
    acct = _S.get_active(data_dir)
    if not acct:
        _print("本地无活动账号：请先运行 init")
        return 1
    _print("== 选座系统会话 Cookie 导入 ==")
    _print("1) 手机抓包（iOS:Stream / Android:HttpCanary）后打开微信「浙商大图书馆助手」选座系统；")
    _print("2) 在抓包记录里找任意一条 wechatseat.zjgsu.edu.cn 请求；")
    _print("3) 复制其请求头 Cookie 的完整值（形如 wechatSESS_ID=…; FROM_TYPE=weixin; …）。")
    cookie = getpass.getpass("粘贴 Cookie（输入无回显）: ").strip()
    if not cookie or "wechatSESS" not in cookie:
        _print("Cookie 无效：未包含 wechatSESS 会话项（抓包时请先在微信里打开选座页面）。")
        return 1
    sess = _S.load_session(data_dir, acct) or {}
    sess["seat_cookie"] = cookie
    sess["seat_cookie_saved_at"] = _now_s()
    _S.save_session(data_dir, acct, sess)
    _print("选座会话 Cookie 已写入本机会话（%s）。" % _now_s())
    _print("不打印、不进日志；查询报 session_expired 时请重新抓包并重跑本命令。")
    return 0


def cmd_seat_check() -> int:
    """只读自检：用已导入 Cookie 拉选座系统楼层占用汇总（无写操作）。"""
    from pathlib import Path
    import src.auth.session as _S
    from ..adapters.campus.seat import fetch_seat_summary
    data_dir = (Path(os.environ.get("ZJGSU_CAMPUS_DATA_DIR"))
                if os.environ.get("ZJGSU_CAMPUS_DATA_DIR")
                else Path(__file__).resolve().parents[2] / "data")
    acct = _S.get_active(data_dir)
    if not acct:
        _print("本地无活动账号：请先运行 init")
        return 1
    sess = _S.load_session(data_dir, acct) or {}
    cookie = sess.get("seat_cookie")
    if not cookie:
        _print("未导入选座会话 Cookie：请先运行 seat-cookie")
        return 1
    res = fetch_seat_summary(cookie)
    _print("== 选座系统只读自检 ==")
    _print("outcome: %s" % res.get("outcome"))
    if res.get("diag"):
        _print("diag: %s" % res["diag"])
    data = res.get("data") or {}
    if res.get("outcome") == "ok":
        _print("")
        _print("按楼层（空位/总数）：")
        order = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
        for fl in sorted(data.get("by_floor") or {},
                         key=lambda x: order.get(x[:1], 99)):
            a = data["by_floor"][fl]
            rate = a["free"] / a["total"] * 100 if a["total"] else 0
            _print("%s楼：%d / %d（空位率 %.0f%%）"
                   % (fl, a["free"], a["total"], rate))
            for area in a["areas"]:
                _print("   · %s：%d / %d" % (area["name"], area["free"],
                                             area["total"]))
        _print("")
        _print("空位率 TOP3：")
        for r in (data.get("rank_by_rate") or [])[:3]:
            _print("  %s（%s）：%d/%d（%.0f%%）"
                   % (r["name"], r["floor"], r["free"], r["total"],
                      r["free_rate"] * 100))
    return 0 if res.get("outcome") == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else "init"
    if mode == "init":
        return cmd_init()
    if mode == "status":
        return cmd_status()
    if mode == "logout":
        return cmd_logout()
    if mode == "fitness-token":
        return cmd_fitness_token()
    if mode == "seat-cookie":
        return cmd_seat_cookie()
    if mode == "seat-check":
        return cmd_seat_check()
    _print("用法：python -m src.auth.login_cli [init|status|logout|fitness-token|seat-cookie|seat-check]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
