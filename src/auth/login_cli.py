# -*- coding: utf-8 -*-
"""本机登录初始化 CLI（认证初始化与 MCP 请求协议分离）。

用法（在项目根目录、交互式终端中运行）：
    .venv\\Scripts\\python.exe -m src.auth.login_cli init     # 登录并保存本机会话（默认）
    .venv\\Scripts\\python.exe -m src.auth.login_cli init --save-cred [天数]  # 登录成功后顺带保存凭据
    .venv\\Scripts\\python.exe -m src.auth.login_cli cred-save      # 保存统一认证凭据（供自动重登）
    .venv\\Scripts\\python.exe -m src.auth.login_cli cred-status    # 查看凭据状态（掩码，不含秘密）
    .venv\\Scripts\\python.exe -m src.auth.login_cli cred-clear     # 清除已保存凭据
    .venv\\Scripts\\python.exe -m src.auth.login_cli autologin     # 用已存凭据非交互刷新会话
    .venv\\Scripts\\python.exe -m src.auth.login_cli status   # 查看会话状态（不含秘密）
    .venv\\Scripts\\python.exe -m src.auth.login_cli logout   # 清除本机会话（缓存随之不可读）

边界（对应探针 P01/P05 与交接文档第 7 节，及 F29 凭据存储设计）：
- init 路径：凭据仅在本人交互式终端输入（非 TTY 拒绝）；密码不回显、不写日志；
- cred-save 路径：同样交互式输入（非 TTY 拒绝）；凭据写入 data/credentials.json
  （本机私有目录 + .gitignore；明文 JSON，与会话文件同级别边界；原子写）；
- autologin 路径：非交互、无参数——凭据来自本机凭据层文件，不经过 stdin/管道，
  全程不回显不打印；凭据缺失/超期/上次失败时拒绝执行并明确提醒更新；
- 本 CLI 与 MCP 服务分离：MCP 工具永远不会询问或接收凭据；
  MCP 服务端仅在「会话失效 + 凭据层可用」时自动重登（见 src/service/auto_relogin.py）；
- 会话文件（含 zjgsuToken）写入 data/sessions/（私有目录，.gitignore 排除）；
- 输出不含 token、密码；用户名仅以掩码显示。
"""
from __future__ import annotations

import getpass
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..auth.session import (account_scope_id, clear_active, delete_session, get_active,
                            load_session)
from ..paths import default_data_dir
from .auto_login import check_environment, login_and_save_session
from .credential_store import (DEFAULT_TTL_DAYS, CredentialWriteError, clear_credentials,
                               evaluate, load_credentials, mark_login_failed,
                               mark_login_ok, save_credentials, status_for_display)
from .file_lock import authentication_lock

TZ = timezone(timedelta(hours=8))


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
        _print("会话：缺失（请先运行 init 或 autologin）")
        return 1
    _print("会话：存在")
    _print("  用户类型：%s（2=学生）" % session.get("user_type"))
    _print("  建立时间：%s" % session.get("created_at"))
    return 0


def cmd_logout() -> int:
    data_dir = default_data_dir()
    with authentication_lock(data_dir):
        acct = get_active(data_dir)
        if acct:
            delete_session(data_dir, acct)
            clear_active(data_dir)
    if acct:
        _print("本机会话已清除（账号域 %s）。" % acct)
    else:
        _print("本来就没有生效会话。")
    _print("说明：缓存数据行仍在本机 SQLite 中，但在无会话状态下不可通过工具读取；重新登录后恢复。")
    return 0


def _print_login_result(result: dict, acct_hint: str | None = None) -> int:
    """打印 login_and_save_session 的结果（init 与 autologin 共用；不含秘密）。"""
    if result["outcome"] == "environment":
        _print("已停止（环境不可用）：%s" % result["diag"])
        _print("（凭据未验证；自动登录熔断状态不变。）")
        return 2
    if result["outcome"] != "ok":
        _print("登录未完成：%s" % result["diag"])
        return 1
    _print("教务网页版会话 ✓（jwglxt_cookies 已入会话）"
           if result.get("jwweb_ok")
           else "教务网页版会话未建立（%s）；教务网页版工具将不可用，可稍后重跑刷新。"
                % (result.get("jwweb_diag") or "原因未知"))
    if result.get("kept_independent"):
        _print("已保留本机独立凭据：%s（未重新获取）"
               % "、".join(result["kept_independent"]))
    _print("登录成功：%s" % result.get("diag", ""))
    if result.get("identity_changed"):
        _print("注意：身份类型发生变化（%s → %s）；涉身份口径的工具输出以新会话为准。"
               % (result.get("old_user_type"), result.get("user_type")))
    if str(result.get("user_type")) != "2":
        _print("注意：当前身份不是学生（user_type=%s），课表模块将显示为不可用。"
               % result.get("user_type"))
    if acct_hint:
        _print("会话已保存（私有目录）；账号域标识：%s" % acct_hint)
    return 0


def cmd_init(save_cred_ttl: int | None = None) -> int:
    data_dir = default_data_dir()

    _print("== 「我的商大」登录初始化（本机） ==")
    _print("凭据仅用于本机向学校接口发起登录；密码不回显、不写日志。")
    if save_cred_ttl:
        _print("已启用 --save-cred：登录成功后凭据将写入本机凭据层（data/credentials.json，"
               "时效 %s 天，供会话失效时自动重登）。" % save_cred_ttl)
    _print()

    # —— 工具前置检查（在询问凭据之前） ——
    constants, env_err = check_environment(data_dir)
    if constants is None:
        _print("已停止：%s" % env_err)
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
    result = login_and_save_session(data_dir, username, password)

    _print()
    if result["outcome"] != "ok":
        code = _print_login_result(result)
        # R25：所有认证路径结束后丢弃明文密码引用
        del password
        _print("（未保存任何会话；如需重试请重新运行 init。）")
        return code

    # —— F29：可选保存凭据（登录成功后；保存完成立即丢弃明文引用） ——
    saved_cred = False
    if save_cred_ttl:
        try:
            save_credentials(data_dir, username, password, ttl_days=save_cred_ttl)
            saved_cred = True
        except ValueError as e:
            _print("凭据保存失败（参数无效）：%s" % e)
        except CredentialWriteError as e:
            # R3：登录会话本身成功，但凭据未保存——两者必须分开说明
            _print("登录会话成功，但凭据未能保存：%s" % e)
            _print("（本次会话可用；下次会话失效仍需手动 init 或重新 cred-save。）")
    # R25：两次认证与凭据保存全部结束后才丢弃明文密码引用
    del password

    code = _print_login_result(result, acct_hint=acct)
    if saved_cred:
        st = status_for_display(data_dir)
        _print("凭据已写入本机凭据层（用户名 %s，时效 %s 天）："
               "此后会话失效时 MCP 工具将自动重登，无需人工干预。"
               % (st.get("username_masked"), st.get("ttl_days")))
    return code


def cmd_cred_save() -> int:
    """F29：交互式保存统一认证凭据（供会话失效时自动重登）。"""
    data_dir = default_data_dir()
    _print("== 保存统一认证凭据（本机凭据层） ==")
    _print("用途：MCP 会话失效时自动重登；文件 data/credentials.json（本机私有 + .gitignore，"
           "明文 JSON，与会话文件同级别边界）。")
    _print("时效：超过保存时效（默认 %d 天）后自动登录将拒绝使用并提醒更新，防止旧密码撞锁。" % DEFAULT_TTL_DAYS)
    _print()
    if not sys.stdin.isatty():
        _print("cred-save 需要交互式输入（当前非终端环境）——请在真实终端运行")
        return 2
    try:
        username = input("用户名（学号/工号）：").strip()
        password = getpass.getpass("密码（不回显）：")
        ttl_raw = input("时效（天，回车=默认 %d）：" % DEFAULT_TTL_DAYS).strip()
    except (KeyboardInterrupt, EOFError):
        _print("已取消输入。")
        return 1
    if not username or not password:
        _print("用户名或密码为空，已退出。")
        return 1
    ttl_days = int(ttl_raw) if ttl_raw.isdigit() and int(ttl_raw) > 0 else DEFAULT_TTL_DAYS
    try:
        save_credentials(data_dir, username, password, ttl_days=ttl_days)
    except ValueError as e:
        _print("保存失败：%s" % e)
        return 1
    except CredentialWriteError as e:
        # R3：写入失败必须非零退出且明确“未保存”（不得打印“凭据已保存”）
        _print("凭据未保存：%s" % e)
        _print("（磁盘上仍是原来的凭据；如为首次保存，当前没有可用凭据。）")
        return 1
    st = status_for_display(data_dir)
    _print()
    _print("凭据已保存：用户名 %s、时效 %s 天、保存时间 %s。"
           % (st.get("username_masked"), st.get("ttl_days"), st.get("saved_at")))
    _print("密码不回显、不写日志；如需清除运行 cred-clear。")
    _print("此后 MCP 工具遇到会话失效将自动用该凭据重登（一次），无需人工干预。")
    return 0


def cmd_cred_status() -> int:
    data_dir = default_data_dir()
    st = status_for_display(data_dir)
    _print("== 本机凭据状态 ==")
    if not st.get("present"):
        _print("凭据：未保存（%s）" % st.get("reason"))
        _print("如需自动重登能力：运行 cred-save 或 init --save-cred。")
        return 1
    _print("凭据：存在（用户名 %s）" % st.get("username_masked"))
    _print("  保存时间：%s；时效：%s 天（已存 %s 天）"
           % (st.get("saved_at"), st.get("ttl_days"), st.get("age_days")))
    _print("  可自动使用：%s（%s）" % ("是" if st.get("usable") else "否", st.get("reason")))
    if st.get("last_login_ok_at"):
        _print("  最近自动登录成功：%s" % st.get("last_login_ok_at"))
    if st.get("last_fail_at"):
        _print("  最近失败：%s（%s）" % (st.get("last_fail_at"),
                                        st.get("last_fail_diag") or "无诊断"))
    return 0 if st.get("usable") else 1


def cmd_cred_clear() -> int:
    data_dir = default_data_dir()
    if clear_credentials(data_dir):
        _print("凭据已清除（data/credentials.json）。")
    else:
        _print("本来就没有已保存的凭据。")
    return 0


def cmd_autologin() -> int:
    """F29：非交互自动登录——凭据来自本机凭据层（不经过 stdin/管道）。

    凭据缺失/损坏/超期/上次失败（熔断）→ 拒绝执行并提醒更新（防旧密码撞锁号）；
    登录被拒 → 标记熔断；网络/环境问题 → 不熔断。"""
    data_dir = default_data_dir()
    _print("== 「我的商大」自动登录（本机凭据层） ==")
    cred = load_credentials(data_dir)
    ev = evaluate(cred)
    if not ev.get("usable"):
        reason = ev.get("reason")
        if reason in ("credentials_missing", "credentials_invalid"):
            _print("已停止：本机凭据层%s。"
                   % ("未保存凭据" if reason == "credentials_missing" else "凭据文件损坏"))
            _print("请运行 cred-save（或 init --save-cred）保存凭据。")
        elif reason == "credentials_expired":
            _print("已停止：凭据已超期（已存 %s 天 > 时效 %s 天），可能已改密。"
                   % (ev.get("age_days"), (cred or {}).get("ttl_days")))
            _print("如密码未变可重新保存：cred-save；已改密请先更新后再 autologin。")
        elif reason == "credentials_needs_update":
            _print("已停止：上次自动登录失败（%s），已熔断以防连续失败锁号。"
                   % ((cred or {}).get("last_fail_diag") or "原因见 cred-status"))
            _print("请确认密码后运行 cred-save 更新凭据（更新将自动解除熔断）。")
        return 2
    _print("凭据检查通过（%s），开始登录…" % ev.get("reason"))
    username = cred["username"]
    password = cred["password"]
    credential_version = cred["version"]
    result = login_and_save_session(data_dir, username, password)
    del password, username
    _print()
    if result["outcome"] == "ok":
        persisted = mark_login_ok(data_dir, credential_version)
        code = _print_login_result(result, acct_hint=result.get("account_scope_id"))
        _print("自动登录完成：凭据层已记录最近成功时间。" if persisted else
               "登录完成；凭据已更新、清除或暂不可写，旧版本结果未写入当前凭据。")
        return code
    if result["outcome"] == "auth_failed":
        persisted = mark_login_failed(data_dir, result.get("diag", ""), credential_version)
        _print_login_result(result)
        if persisted:
            _print("凭据已标记「待更新」并熔断：自动登录将拒绝再试（防止连续失败锁号）。")
            _print("请确认密码后运行 cred-save 更新（自动解除熔断）。")
        else:
            _print("本次旧版本登录失败；凭据已更新、清除或暂不可写，当前凭据状态未被修改。")
        return 1
    # network / environment：不熔断
    code = _print_login_result(result)
    if result["outcome"] == "environment":
        _print("说明：环境问题与凭据无关，熔断状态未变。")
    else:
        _print("说明：网络等非凭据因素，熔断状态未变；可稍后重试 autologin。")
    return code


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
    with authentication_lock(data_dir):
        sess = _S.load_session(data_dir, acct)
        if _S.get_active_file(data_dir) != acct or not sess:
            _print("输入期间账号已切换或退出，未保存令牌；请重新运行本命令。")
            return 1
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
    with authentication_lock(data_dir):
        sess = _S.load_session(data_dir, acct)
        if _S.get_active_file(data_dir) != acct or not sess:
            _print("输入期间账号已切换或退出，未保存Cookie；请重新运行本命令。")
            return 1
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


def _parse_save_cred_flag(rest: list[str]) -> int | None:
    """解析 init 的 --save-cred [天数]（缺省默认时效；非数字回退默认）。"""
    if "--save-cred" not in rest:
        return None
    idx = rest.index("--save-cred")
    tail = rest[idx + 1: idx + 2]
    if tail and tail[0].isdigit() and int(tail[0]) > 0:
        return int(tail[0])
    return DEFAULT_TTL_DAYS


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else "init"
    rest = args[1:]
    if mode == "init":
        return cmd_init(save_cred_ttl=_parse_save_cred_flag(rest))
    if mode == "autologin":
        return cmd_autologin()
    if mode == "cred-save":
        return cmd_cred_save()
    if mode == "cred-status":
        return cmd_cred_status()
    if mode == "cred-clear":
        return cmd_cred_clear()
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
    _print("用法：python -m src.auth.login_cli "
           "[init|autologin|cred-save|cred-status|cred-clear|status|logout|"
           "fitness-token|seat-cookie|seat-check]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
