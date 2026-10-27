# -*- coding: utf-8 -*-
"""F29 自动重登：审查反例转正式回归（不触网）。

覆盖 validation/review-auto-relogin-2026-10-19 的 6 组反例：
- P1-1：真实注册工具（CallToolResult 边界）必须进入重登分支；重试后文本/结构化/isError 一致；
- P1-2：账号绑定——凭据属于另一账号时不得登录/切号/重放；
- P1-3：并发同批只登录一次；认证拒绝先内存熔断（持久化失败不撞登录）；同秒等长改密可再试；
- P2-4：独立凭据域（体测 token）不触发统一认证重登；
- P2-5：损坏元数据受控拒绝（store 测试已覆盖，这里验证 attempt 层 skipped）；
- 其余：skipped/ok/failed 语义、hint 文案、签名保持。

import src.mcp_server.server 有模块级副作用（SnapshotStore 建库），测试统一在
import 前把 ZJGSU_CAMPUS_DATA_DIR 指到临时隔离目录；装饰器运行时再 patch DATA_DIR。
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from test.helpers import PROJECT_ROOT, temp_dir

# —— 在 import src.mcp_server.server 之前固定隔离数据目录 ——
_ISOLATED_DATA = temp_dir()
os.environ["ZJGSU_CAMPUS_DATA_DIR"] = _ISOLATED_DATA.name

from src.auth import auto_login as L               # noqa: E402
from src.auth import credential_store as C         # noqa: E402
from src.auth import session as S                  # noqa: E402
from src.service import auto_relogin as A          # noqa: E402


def _auth_wrapper():
    return {"status": "unavailable",
            "issues": [{"code": "auth_required", "retryable": False,
                        "safe_message": "（合成）会话失效"}]}


def _ok_wrapper():
    return {"status": "ok", "items": [], "issues": []}


def _code99_wrapper(status="partial", hinted=True):
    """App 域会话失效：业务码 99（结构化 auth_hint 由服务层按来源/端点设置）。"""
    item = {"code": "business_error", "retryable": False,
            "safe_message": "code=99（疑似会话/认证问题；语义未验证，"
                            "不直接映射 auth_required）"}
    if hinted:
        item["auth_hint"] = "app_session"
    return {"status": status, "items": [], "issues": [item]}


def _code(wrapper):
    return [i["code"] for i in (wrapper.get("issues") or [])]


def _reset():
    A._LAST.update(cred_fp=None, outcome=None, at=None)


def _write_cred_raw(data_dir: Path, cred: dict) -> None:
    (data_dir / C.FILENAME).write_text(json.dumps(cred, ensure_ascii=False),
                                       encoding="utf-8")


class AttemptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _reset()

    def tearDown(self):
        _reset()

    def test_skipped_when_no_credentials(self):
        res = A.attempt_auto_relogin(self.data_dir)
        self.assertEqual(res["outcome"], "skipped")
        self.assertEqual(res["reason"], C.REASON_MISSING)

    def test_skipped_when_corrupt_or_invalid_metadata(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        valid = C.load_credentials(self.data_dir)
        for updates in ({"saved_at": None}, {"saved_at": "bad"},
                        {"saved_at": "2026-10-19T12:00:00"},
                        {"ttl_days": "bad"}, {"version": ""}):
            cred = dict(valid, **updates)
            _write_cred_raw(self.data_dir, cred)
            res = A.attempt_auto_relogin(self.data_dir)
            self.assertEqual(res["outcome"], "skipped", updates)
            self.assertEqual(res["reason"], C.REASON_MISSING, updates)

    def test_skipped_when_expired(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass", ttl_days=10)
        cred = C.load_credentials(self.data_dir)
        cred["saved_at"] = "2025-01-01T00:00:00+08:00"  # 远超 TTL
        _write_cred_raw(self.data_dir, cred)
        res = A.attempt_auto_relogin(self.data_dir)
        self.assertEqual(res["outcome"], "skipped")
        self.assertEqual(res["reason"], C.REASON_EXPIRED)

    def test_skipped_when_needs_update(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        C.mark_login_failed(self.data_dir, "密码错误")
        res = A.attempt_auto_relogin(self.data_dir)
        self.assertEqual(res["outcome"], "skipped")
        self.assertEqual(res["reason"], C.REASON_NEEDS_UPDATE)

    def test_ok_updates_credential_state(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        with mock.patch.object(A, "login_and_save_session",
                               return_value={"outcome": "ok", "diag": "登录成功",
                                             "jwweb_ok": True}) as m:
            res = A.attempt_auto_relogin(self.data_dir)
        self.assertEqual(res["outcome"], "ok")
        self.assertTrue(res["jwweb_ok"])
        m.assert_called_once()
        cred = C.load_credentials(self.data_dir)
        self.assertIsNotNone(cred["last_login_ok_at"])
        self.assertFalse(cred["needs_update"])

    def test_network_failure_no_circuit_break(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        with mock.patch.object(A, "login_and_save_session",
                               return_value={"outcome": "network", "diag": "timeout"}):
            res = A.attempt_auto_relogin(self.data_dir)
        self.assertEqual(res["outcome"], "failed")
        self.assertFalse(res["auth_failed"])
        self.assertFalse(C.load_credentials(self.data_dir)["needs_update"])

    def test_login_exception_bounded(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        with mock.patch.object(A, "login_and_save_session",
                               side_effect=RuntimeError("boom")):
            res = A.attempt_auto_relogin(self.data_dir)
        self.assertEqual(res["outcome"], "failed")
        self.assertFalse(res["auth_failed"])

    # ---------- P1-2：账号绑定 ----------
    def test_account_binding_never_switches_active(self):
        acct_b = S.account_scope_id("synthetic-b", "synthetic-client")
        S.save_session(self.data_dir, acct_b,
                       {"schema": 1, "zjgsu_token": "synthetic-token-b"})
        S.set_active(self.data_dir, acct_b)
        C.save_credentials(self.data_dir, "synthetic-a", "synthetic-password")

        with mock.patch.object(L, "check_environment",
                               return_value=({"client_id": "synthetic-client"}, None)), \
             mock.patch.object(L, "find_java", return_value="synthetic-java"), \
             mock.patch.object(L.campus_auth, "perform_login",
                               return_value={"outcome": "ok", "token": "synthetic-token-a",
                                             "user_type": "2", "diag": "ok"}) as login, \
             mock.patch("src.adapters.campus.jwweb.cas_login",
                        return_value={"outcome": "ok",
                                      "cookies": [{"name": "synthetic"}]}):
            res = A.attempt_auto_relogin(self.data_dir)  # expected 默认 = 当前 active
        self.assertEqual(res["outcome"], "skipped")
        self.assertEqual(res["reason"], "account_mismatch")
        login.assert_not_called()                       # 不得登录另一账号
        self.assertEqual(S.get_active(self.data_dir), acct_b)  # 不得切换 active

    def test_account_binding_explicit_expected(self):
        C.save_credentials(self.data_dir, "synthetic-a", "synthetic-password")
        with mock.patch.object(L, "check_environment",
                               return_value=({"client_id": "synthetic-client"}, None)):
            res = A.attempt_auto_relogin(
                self.data_dir,
                expected_account=S.account_scope_id("synthetic-b", "synthetic-client"))
        self.assertEqual(res["outcome"], "skipped")
        self.assertEqual(res["reason"], "account_mismatch")

    # ---------- P1-3：并发去重 ----------
    def test_concurrent_requests_login_once(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        _reset()
        entered, loaded, release = threading.Event(), threading.Event(), threading.Event()
        original_load = C.load_credentials
        counter = {"calls": 0}

        def tracked_load(path):
            cred = original_load(path)
            if threading.current_thread().name == "review-second":
                loaded.set()
            return cred

        def fake_login(*args, **kwargs):
            counter["calls"] += 1
            if counter["calls"] == 1:
                entered.set()
                self.assertTrue(release.wait(5))
            return {"outcome": "ok", "diag": "ok", "jwweb_ok": True}

        results = []
        with mock.patch.object(C, "load_credentials", side_effect=tracked_load), \
             mock.patch.object(A, "login_and_save_session", side_effect=fake_login):
            first = threading.Thread(
                target=lambda: results.append(A.attempt_auto_relogin(self.data_dir)),
                name="review-first")
            second = threading.Thread(
                target=lambda: results.append(A.attempt_auto_relogin(self.data_dir)),
                name="review-second")
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            self.assertTrue(loaded.wait(5))   # 第二请求能在锁外读到凭据
            release.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(counter["calls"], 1)  # 同批只登录一次
        self.assertTrue(any(r.get("reused") for r in results))

    # ---------- P1-3：持久化失败时熔断仍生效 ----------
    def test_failed_state_write_circuit_breaks_without_error(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        _reset()
        errors = []
        with mock.patch.object(A, "login_and_save_session",
                               return_value={"outcome": "auth_failed", "diag": "rejected"}) as login, \
             mock.patch.object(C, "mark_login_failed", side_effect=OSError("synthetic disk error")):
            for _ in range(2):
                try:
                    A.attempt_auto_relogin(self.data_dir)
                except Exception as exc:  # 不得向上抛
                    errors.append(type(exc).__name__)
        self.assertEqual(errors, [])
        self.assertEqual(login.call_count, 1)  # 内存熔断：第二次不再撞登录

    # ---------- P1-3：同秒等长改密可再试 ----------
    def test_same_second_equal_length_resave_can_retry(self):
        _reset()
        with mock.patch.object(C, "_now_s", return_value="2026-10-19T12:00:00+08:00"), \
             mock.patch.object(A, "login_and_save_session", side_effect=[
                 {"outcome": "auth_failed", "diag": "rejected"},
                 {"outcome": "ok", "diag": "ok", "jwweb_ok": True}]) as login:
            C.save_credentials(self.data_dir, "synthetic-user", "synthetic-old")
            A.attempt_auto_relogin(self.data_dir)
            C.save_credentials(self.data_dir, "synthetic-user", "synthetic-new")  # 等长、同秒
            result = A.attempt_auto_relogin(self.data_dir)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(login.call_count, 2)

    # ---------- F29-R1/复用：会话被替换时不得复用成功结果 ----------
    def test_reuse_invalidated_when_session_replaced(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        acct = S.account_scope_id("synthetic-user", "synthetic-client")
        S.save_session(self.data_dir, acct,
                       {"schema": 1, "zjgsu_token": "token-initial"})
        _reset()
        calls = {"n": 0}

        def fake_login(*args, **kwargs):
            calls["n"] += 1
            S.save_session(self.data_dir, acct,
                           {"schema": 1, "zjgsu_token": "token-%d" % (calls["n"] + 1)})
            return {"outcome": "ok", "diag": "ok", "jwweb_ok": True}

        with mock.patch.object(L, "check_environment",
                               return_value=({"client_id": "synthetic-client"}, None)), \
             mock.patch.object(A, "login_and_save_session", side_effect=fake_login):
            r1 = A.attempt_auto_relogin(self.data_dir, acct)
            r2 = A.attempt_auto_relogin(self.data_dir, acct)   # 会话未变 → 复用
            S.save_session(self.data_dir, acct,                # 会话被替换
                           {"schema": 1, "zjgsu_token": "token-broken"})
            r3 = A.attempt_auto_relogin(self.data_dir, acct)   # 不得复用，须重登
        self.assertEqual(r1["outcome"], "ok")
        self.assertTrue(r2.get("reused"))
        self.assertEqual(r3["outcome"], "ok")
        self.assertFalse(r3.get("reused"))
        self.assertEqual(calls["n"], 2)

    # ---------- F29-R1：登录期间切号不得覆盖用户选择 ----------
    def test_switch_during_login_does_not_override_selection(self):
        acct_a = S.account_scope_id("synthetic-a", "synthetic-client")
        acct_b = S.account_scope_id("synthetic-b", "synthetic-client")
        C.save_credentials(self.data_dir, "synthetic-a", "synthetic-password")

        def login_during_switch(*args):
            S.set_active(self.data_dir, acct_b)  # 登录进行中：用户显式切号
            return {"outcome": "ok", "token": "synthetic-token",
                    "user_type": "2", "diag": "ok"}

        _reset()
        with mock.patch.object(L, "check_environment",
                               return_value=({"client_id": "synthetic-client"}, None)), \
             mock.patch.object(L, "find_java", return_value="synthetic-java"), \
             mock.patch.object(L.campus_auth, "perform_login",
                               side_effect=login_during_switch), \
             mock.patch("src.adapters.campus.jwweb.cas_login",
                        return_value={"outcome": "ok", "cookies": []}):
            res = A.attempt_auto_relogin(self.data_dir, expected_account=acct_a)
        self.assertEqual(res["outcome"], "skipped")
        self.assertEqual(res["reason"], "context_changed")
        # 发布前校验：不覆盖用户刚做的切号
        self.assertEqual(S.get_active_file(self.data_dir), acct_b)

    # ---------- F29-R2：旧登录结果不得污染新保存的凭据 ----------
    def test_credential_resave_during_login_not_marked_failed(self):
        C.save_credentials(self.data_dir, "synthetic-a", "synthetic-old")
        _reset()

        def old_login_rejected(*args):
            # 旧密码登录期间，用户重新保存了新密码（新 version）
            C.save_credentials(self.data_dir, "synthetic-a", "synthetic-new")
            return {"outcome": "auth_failed", "diag": "synthetic rejected"}

        with mock.patch.object(A, "login_and_save_session",
                               side_effect=old_login_rejected):
            A.attempt_auto_relogin(self.data_dir)
        after = C.load_credentials(self.data_dir)
        self.assertFalse(after["needs_update"])  # 旧失败不得熔断新凭据
        self.assertEqual(after["fail_count"], 0)


class HintTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_hint_missing(self):
        self.assertIn("cred-save", A.credential_issue_hint(self.data_dir)["hint"])

    def test_hint_expired(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass", ttl_days=1)
        cred = C.load_credentials(self.data_dir)
        cred["saved_at"] = "2025-01-01T00:00:00+08:00"
        _write_cred_raw(self.data_dir, cred)
        self.assertIn("超期", A.credential_issue_hint(self.data_dir)["hint"])

    def test_hint_account_mismatch(self):
        hint = A.credential_issue_hint(self.data_dir, reason="account_mismatch")["hint"]
        self.assertIn("不会切换账号", hint)

    def test_hint_environment(self):
        hint = A.credential_issue_hint(self.data_dir, reason="environment")["hint"]
        self.assertIn("环境", hint)


class ServerWrapperTest(unittest.TestCase):
    """真实注册工具 + 真实 CallToolResult 边界（不再用自造字典替身）。"""

    @classmethod
    def setUpClass(cls):
        from src.mcp_server import server
        cls.server = server

    def setUp(self):
        self._tmp = temp_dir()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _reset()

    def tearDown(self):
        _reset()

    def _patches(self, attempt_result, service_returns=None):
        """patch 缓存层返回序列（真实注册函数与 _to_call_result 边界保持生产实现）。"""
        srv = self.server
        seq = list(service_returns or [_ok_wrapper()])
        calls = {"n": 0}

        def fake_cache_call(method, *args, **kwargs):
            calls["n"] += 1
            idx = min(calls["n"], len(seq)) - 1
            return seq[idx]

        return [
            mock.patch.object(srv, "DATA_DIR", self.data_dir),
            mock.patch.object(srv, "get_active_file", return_value=None),
            mock.patch.object(srv, "attempt_auto_relogin", return_value=attempt_result),
            mock.patch.object(srv.query_cache, "call", side_effect=fake_cache_call),
        ], calls

    def test_real_tool_calltoolresult_retry_success(self):
        """P1-1：真实返回类型 CallToolResult 必须进入重登并重放成功。"""
        patches, calls = self._patches(
            {"outcome": "ok", "diag": "ok"},
            [_auth_wrapper(), _ok_wrapper()])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_grades("2026", "3")
        self.assertEqual(calls["n"], 2)                       # 恰好重放一次
        self.assertFalse(result.is_error)                     # ok → isError=False
        structured = result.structured_content
        self.assertEqual(structured["status"], "ok")
        self.assertIn(self.server.ISSUE_AUTO_RELOGIN, _code(structured))
        # 文本与结构化内容同源一致（重建自同一字典）
        self.assertEqual(json.loads(result.content[0].text), structured)

    def test_real_tool_retry_still_auth_not_reported_as_success(self):
        """P1-1：重试仍失败时不得写成“成功、无需人工处理”。"""
        patches, calls = self._patches(
            {"outcome": "ok", "diag": "ok"},
            [_auth_wrapper(), _auth_wrapper()])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_grades("2026", "3")
        self.assertEqual(calls["n"], 2)                       # 只重放一次，不连环重试
        self.assertTrue(result.is_error)
        structured = result.structured_content
        notes = [i["safe_message"] for i in structured["issues"]
                 if i["code"] == self.server.ISSUE_AUTO_RELOGIN]
        # R5：按最终 status 描述——unavailable 必须保留失败，不得宣称成功
        self.assertTrue(notes and "仍失败" in notes[0])
        self.assertNotIn("无需人工处理", notes[0])
        self.assertEqual(json.loads(result.content[0].text), structured)

    def test_real_tool_failed_attempt_appends_reminder(self):
        """真实工具 + 重登失败：追加 credential_update_required，结构一致。"""
        patches, calls = self._patches(
            {"outcome": "failed", "auth_failed": True, "diag": "rejected"},
            [_auth_wrapper()])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_grades("2026", "3")
        self.assertEqual(calls["n"], 1)                       # 失败不重放
        self.assertTrue(result.is_error)
        structured = result.structured_content
        self.assertIn(self.server.ISSUE_CREDENTIAL_UPDATE, _code(structured))
        self.assertEqual(json.loads(result.content[0].text), structured)

    def test_real_tool_no_credentials_untouched(self):
        """未启用凭据层：既有行为零改动（不调 attempt、不追加 issue）。"""
        srv = self.server
        calls = {"n": 0}

        def fake_cache_call(method, *args, **kwargs):
            calls["n"] += 1
            return _auth_wrapper()

        # 不 patch attempt：走真实 attempt → 空目录 → skipped(credentials_missing)
        with mock.patch.object(srv, "DATA_DIR", self.data_dir), \
             mock.patch.object(srv, "get_active_file", return_value=None), \
             mock.patch.object(srv.query_cache, "call", side_effect=fake_cache_call):
            result = srv.get_grades("2026", "3")
        self.assertEqual(calls["n"], 1)
        structured = result.structured_content
        self.assertEqual(_code(structured), ["auth_required"])
        self.assertEqual(json.loads(result.content[0].text), structured)

    def test_independent_fitness_auth_not_triggered(self):
        """P2-4：体测 token 缺失（independent 域）不进入统一认证重登。"""
        srv = self.server
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        with mock.patch.object(srv, "DATA_DIR", self.data_dir), \
             mock.patch.object(srv, "attempt_auto_relogin",
                               return_value={"outcome": "ok"}) as attempt, \
             mock.patch.object(srv.jwweb_service, "_ctx",
                               return_value=("synthetic-acct",
                                             {"zjgsu_token": "synthetic-token"})):
            fitness = srv.jwweb_service.get_fitness_scores()  # 真实服务：缺 tzjk_token
            self.assertEqual(fitness["status"], "unavailable")
            self.assertFalse(srv._has_auth_issue(fitness))    # 独立域 → 不触发
            result = srv.get_fitness_scores()                 # 真实注册函数（CallToolResult）
        attempt.assert_not_called()
        self.assertEqual(result.structured_content["issues"][0].get("auth_domain"),
                         "independent")

    def test_app_domain_code99_triggers_relogin(self):
        """App 域（campus API）会话失效：业务码 99 固定安全文本必须触发自动恢复。"""
        patches, calls = self._patches(
            {"outcome": "ok", "diag": "ok"},
            [_code99_wrapper("partial"), _ok_wrapper()])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_timetable("2026-10-19", "2026-10-25",
                                           force_refresh=True)
        self.assertEqual(calls["n"], 2)                     # 进入并重放一次
        structured = result.structured_content
        self.assertEqual(structured["status"], "ok")
        self.assertIn(self.server.ISSUE_AUTO_RELOGIN, _code(structured))
        self.assertEqual(json.loads(result.content[0].text), structured)

    def test_other_business_error_not_triggered(self):
        """非 99 业务错误（如 code=98）不得触发统一认证重登。"""
        patches, calls = self._patches(
            {"outcome": "ok"},
            [{"status": "unavailable",
              "issues": [{"code": "business_error", "retryable": False,
                          "safe_message": "code=98（业务失败码）"}]}])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_timetable("2026-10-19", "2026-10-25",
                                           force_refresh=True)
        self.assertEqual(calls["n"], 1)
        self.server.attempt_auto_relogin.assert_not_called()
        self.assertEqual(json.loads(result.content[0].text),
                         result.structured_content)

    def test_signature_preserved_for_sdk_schema(self):
        def tool_fn(a: str, b: int = 3) -> dict:
            return _ok_wrapper()
        wrapped = self.server._auto_relogin(tool_fn)
        self.assertEqual(str(inspect.signature(wrapped)),
                         str(inspect.signature(tool_fn)))
        self.assertEqual(wrapped.__name__, "tool_fn")

    # ---------- F29-R1：首次查询中切号，请求仍绑定原账号 ----------
    def test_switch_during_first_query_binds_original_account(self):
        srv = self.server
        acct_a = S.account_scope_id("synthetic-a", "synthetic-client")
        acct_b = S.account_scope_id("synthetic-b", "synthetic-client")
        S.set_active(self.data_dir, acct_a)
        calls = {"n": 0}
        expected_seen = []

        def query():
            calls["n"] += 1
            observed = S.get_active(self.data_dir)   # 通过上下文绑定读取
            if calls["n"] == 1:
                S.set_active(self.data_dir, acct_b)  # 首次查询中切号
                return {"status": "unavailable", "account_scope_id": observed,
                        "issues": [{"code": "auth_required"}]}
            return {"status": "ok", "account_scope_id": observed, "issues": []}

        def fake_attempt(path, expected):
            expected_seen.append(expected)
            return {"outcome": "ok"}

        with mock.patch.object(srv, "DATA_DIR", self.data_dir), \
             mock.patch.object(srv, "attempt_auto_relogin", side_effect=fake_attempt):
            out = srv._auto_relogin(query)()
        self.assertEqual(expected_seen, [acct_a])   # 首次查询之前固定账号
        self.assertEqual(out["account_scope_id"], acct_a)  # 重放不得读新账号

    # ---------- F29-R5：重放按最终状态描述 ----------
    def test_retry_partial_reports_incomplete(self):
        patches, calls = self._patches(
            {"outcome": "ok"},
            [_auth_wrapper(),
             {"status": "partial", "items": [],
              "issues": [{"code": "completeness_unproven"}]}])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_grades("2026", "3")
        notes = [i["safe_message"] for i in result.structured_content["issues"]
                 if i["code"] == self.server.ISSUE_AUTO_RELOGIN]
        self.assertTrue(notes and "partial" in notes[0])
        self.assertNotIn("无需人工处理", notes[0])

    def test_retry_non_auth_failure_not_success(self):
        patches, calls = self._patches(
            {"outcome": "ok"},
            [_auth_wrapper(),
             {"status": "unavailable", "items": [],
              "issues": [{"code": "upstream_error"}]}])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_grades("2026", "3")
        notes = [i["safe_message"] for i in result.structured_content["issues"]
                 if i["code"] == self.server.ISSUE_AUTO_RELOGIN]
        self.assertTrue(notes and "仍失败" in notes[0])
        self.assertNotIn("无需人工处理", notes[0])

    # ---------- F29-R6：99 码必须有结构化来源标记才触发 ----------
    def test_code99_without_structured_hint_not_triggered(self):
        patches, calls = self._patches({"outcome": "ok"},
                                       [_code99_wrapper(hinted=False)])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.server.get_timetable("2026-10-19", "2026-10-25",
                                           force_refresh=True)
        self.assertEqual(calls["n"], 1)
        self.server.attempt_auto_relogin.assert_not_called()

    def test_code99_independent_domain_not_triggered(self):
        wrapper = _code99_wrapper()
        wrapper["issues"][0]["auth_domain"] = "independent"
        patches, calls = self._patches({"outcome": "ok"}, [wrapper])
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server.get_timetable("2026-10-19", "2026-10-25", force_refresh=True)
        self.assertEqual(calls["n"], 1)
        self.server.attempt_auto_relogin.assert_not_called()

    # ---------- F29-R4：体测时段同样属于独立凭据域 ----------
    def test_fitness_schedule_independent_token_not_triggered(self):
        srv = self.server
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        with mock.patch.object(srv, "DATA_DIR", self.data_dir), \
             mock.patch.object(srv, "attempt_auto_relogin",
                               return_value={"outcome": "ok"}) as attempt, \
             mock.patch.object(srv.jwweb_service, "_ctx",
                               return_value=("synthetic-acct",
                                             {"zjgsu_token": "synthetic-token"})):
            result = srv.get_fitness_schedule()
        attempt.assert_not_called()
        structured = result.structured_content
        self.assertEqual(structured["status"], "unavailable")
        self.assertEqual(structured["issues"][0].get("auth_domain"), "independent")


def _stdio_worker():
    """stdio 子进程：合成凭据 + 计数 attempt（不触网；供 StdioRealToolTest 使用）。"""
    from src.auth import credential_store as C
    from src.mcp_server import server

    C.save_credentials(server.DATA_DIR, "synthetic-user", "synthetic-pass")
    counter = Path(os.environ["F29_COUNTER"])

    def fake_attempt(*args):
        counter.write_text(str(int(counter.read_text()) + 1))
        return {"outcome": "ok"}

    with mock.patch.object(server, "attempt_auto_relogin", side_effect=fake_attempt):
        server.mcp.run()


if __name__ == "__main__" and "--f29-stdio-worker" in sys.argv:
    _stdio_worker()
    raise SystemExit(0)


class AuthHintSourceTest(unittest.TestCase):
    """F29-R6 正向对照：只有 App 课表端点 + 会话存在 + 业务码 99 才带结构化标记。"""

    def setUp(self):
        self._tmp = temp_dir()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, response):
        from src.service.campus_service import CampusService
        from src.storage.store import SnapshotStore
        from test.helpers import FakeTransport, seed_session
        seed_session(self.data_dir, "acct-synthetic", user_type="2")
        svc = CampusService(SnapshotStore(self.data_dir / "cache.sqlite3"),
                            self.data_dir, transport=FakeTransport([response]))
        return svc.get_timetable("2026-10-19", "2026-10-25")

    def test_code99_with_session_marks_app_session(self):
        from test.helpers import course_code
        wrapper = self._run(course_code("99"))
        self.assertEqual(wrapper["status"], "unavailable")
        self.assertEqual(wrapper["issues"][0].get("auth_hint"), "app_session")
        self.assertEqual(wrapper["issues"][0].get("code"), "business_error")

    def test_other_business_code_has_no_hint(self):
        from test.helpers import course_code
        wrapper = self._run(course_code("98"))
        self.assertNotIn("auth_hint", wrapper["issues"][0])


class CredSaveCliTest(unittest.TestCase):
    """F29-R3：凭据写入失败必须明确“未保存”，不得报告成功。"""

    def setUp(self):
        self._tmp = temp_dir()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_overwrite_io_failure_reports_unsaved(self):
        from src.auth import login_cli as CLI
        C.save_credentials(self.data_dir, "synthetic-a", "synthetic-old")
        before = C.load_credentials(self.data_dir)
        out = io.StringIO()
        with mock.patch.object(CLI, "default_data_dir", return_value=self.data_dir), \
             mock.patch.object(CLI.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=["synthetic-a", ""]), \
             mock.patch.object(CLI.getpass, "getpass",
                               return_value="synthetic-new"), \
             mock.patch.object(C.os, "replace",
                               side_effect=PermissionError("synthetic denied")), \
             contextlib.redirect_stdout(out):
            code = CLI.cmd_cred_save()
        text = out.getvalue()
        self.assertNotEqual(code, 0)
        self.assertNotIn("凭据已保存", text)
        self.assertIn("未保存", text)
        # 旧凭据未被修改（版本号不变）
        self.assertEqual(C.load_credentials(self.data_dir)["version"],
                         before["version"])

    def test_first_save_io_failure_nonzero(self):
        from src.auth import login_cli as CLI
        out = io.StringIO()
        with mock.patch.object(CLI, "default_data_dir", return_value=self.data_dir), \
             mock.patch.object(CLI.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=["synthetic-a", ""]), \
             mock.patch.object(CLI.getpass, "getpass",
                               return_value="synthetic-new"), \
             mock.patch.object(C.os, "replace",
                               side_effect=PermissionError("synthetic denied")), \
             contextlib.redirect_stdout(out):
            code = CLI.cmd_cred_save()
        self.assertNotEqual(code, 0)
        self.assertIsNone(C.load_credentials(self.data_dir))  # 首次：仍无凭据

    def test_init_session_ok_but_cred_save_failed(self):
        from src.auth import login_cli as CLI
        out = io.StringIO()
        with mock.patch.object(CLI, "default_data_dir", return_value=self.data_dir), \
             mock.patch.object(CLI, "check_environment",
                               return_value=({"client_id": "synthetic-client"}, None)), \
             mock.patch.object(CLI.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="synthetic-a"), \
             mock.patch.object(CLI.getpass, "getpass",
                               return_value="synthetic-pass"), \
             mock.patch.object(CLI, "login_and_save_session",
                               return_value={"outcome": "ok", "diag": "ok",
                                             "account_scope_id": "acct-synthetic",
                                             "user_type": "2", "jwweb_ok": True}), \
             mock.patch.object(CLI, "save_credentials",
                               side_effect=C.CredentialWriteError("synthetic denied")), \
             contextlib.redirect_stdout(out):
            code = CLI.cmd_init(save_cred_ttl=180)
        text = out.getvalue()
        self.assertEqual(code, 0)             # 登录会话成功
        self.assertIn("凭据未能保存", text)    # 但明确区分凭据未保存
        self.assertNotIn("凭据已写入本机凭据层", text)


@unittest.skipIf(os.environ.get("CAMPUS_SKIP_STDIO_TEST") == "1",
                 "CAMPUS_SKIP_STDIO_TEST=1：显式跳过 stdio 集成测试")
class StdioRealToolTest(unittest.IsolatedAsyncioTestCase):
    """真实 stdio + 真实注册工具（P1-1 端到端）：会话失效必须进入重登分支一次。"""

    async def test_sdk_call_enters_relogin_branch(self):
        from mcp.client.client import Client
        from mcp.client.stdio import StdioServerParameters
        tmp_ctx = temp_dir()
        self.addCleanup(tmp_ctx.cleanup)
        tmp = tmp_ctx.name
        counter = Path(tmp) / "attempt-count.txt"
        counter.write_text("0")
        env = dict(os.environ, ZJGSU_CAMPUS_DATA_DIR=tmp,
                   F29_COUNTER=str(counter), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=str(PROJECT_ROOT))
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", str(Path(__file__).resolve()), "--f29-stdio-worker"],
            cwd=tmp, env=env)
        async with Client(params) as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools.tools), 48)
            resp = await client.call_tool(
                "get_grades", {"xnm": "2026", "xqm": "3", "force_refresh": True},
                read_timeout_seconds=20)
        obj = resp.structured_content
        self.assertIsNotNone(obj)
        self.assertEqual(int(counter.read_text()), 1)   # 真实工具进入重登分支恰好一次
        self.assertEqual(obj["status"], "unavailable")
        self.assertTrue(resp.is_error)
        codes = [i["code"] for i in obj["issues"]]
        self.assertIn("auth_required", codes)
        self.assertIn("auto_relogin", codes)            # 已重放（仍失败如实标注）
        self.assertEqual(json.loads(resp.content[0].text), obj)  # 文本/结构化一致


if __name__ == "__main__":
    unittest.main()
