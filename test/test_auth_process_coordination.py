"""Regression tests for MCP/CLI sharing authentication files; auth is synthetic."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from test.helpers import PROJECT_ROOT, temp_dir
from src.auth import auto_login as L, credential_store as C, session as S


def _login(data, username, *expected):
    with patch.object(L, "check_environment", return_value=({"client_id": "synthetic-client"}, None)), \
            patch.object(L, "find_java", return_value="synthetic-java"), \
            patch.object(L.campus_auth, "perform_login", return_value={
                "outcome": "ok", "token": "synthetic-token", "user_type": "2", "diag": "synthetic"}), \
            patch("src.adapters.campus.jwweb.cas_login", return_value={"outcome": "ok", "cookies": []}):
        return L.login_and_save_session(data, username, "synthetic-password", *expected)


def _worker(mode, data, signal):
    signal.with_suffix(".started").touch()
    if mode == "save":
        C.save_credentials(data, "synthetic-a", "synthetic-new-password")
    elif mode == "login":
        assert _login(data, "synthetic-b")["outcome"] == "ok"
    else:
        raise ValueError(mode)
    signal.with_suffix(".done").touch()


def _wait_file(path, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.exists():
            return True
        time.sleep(0.005)
    return path.exists()


def _run_thread(fn, result):
    try:
        result["value"] = fn()
    except BaseException as exc:
        result["error"] = type(exc).__name__


class AuthProcessCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _child(self, mode, data, signal):
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT),
                   ZJGSU_CAMPUS_DATA_DIR=str(data), PYTHONDONTWRITEBYTECODE="1")
        child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                  "--worker", mode, str(data), str(signal)],
                                 cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.assertTrue(_wait_file(signal.with_suffix(".started")))
        return child

    def _finish(self, child):
        _, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err.decode("utf-8", "replace"))

    def test_new_credentials_survive_old_state_commit_in_another_process(self):
        for outcome in ("ok", "failed"):
            with self.subTest(outcome=outcome):
                data = self.base / outcome
                C.save_credentials(data, "synthetic-a", "synthetic-old-password")
                old_version = C.load_credentials(data)["version"]
                ready, release = threading.Event(), threading.Event()
                original = C._safe_write

                def paused(*args):
                    ready.set()
                    if not release.wait(10):
                        raise TimeoutError("synthetic test synchronization")
                    return original(*args)

                operation = (lambda: C.mark_login_ok(data, old_version)) if outcome == "ok" else (
                    lambda: C.mark_login_failed(data, "synthetic rejected", old_version))
                result = {}
                with patch.object(C, "_safe_write", side_effect=paused):
                    thread = threading.Thread(target=_run_thread, args=(operation, result))
                    thread.start()
                    try:
                        self.assertTrue(ready.wait(5))
                        signal = self.base / (outcome + "-signal")
                        child = self._child("save", data, signal)
                        completed_inside_commit = _wait_file(signal.with_suffix(".done"), 0.3)
                    finally:
                        release.set()
                        thread.join(5)
                    self._finish(child)
                self.assertFalse(thread.is_alive())
                self.assertNotIn("error", result)
                self.assertNotEqual(C.load_credentials(data)["version"], old_version)
                self.assertEqual(C.load_credentials(data)["password"], "synthetic-new-password")
                self.assertFalse(completed_inside_commit)

    def test_clear_finishes_after_pending_state_commit_without_resurrecting_credentials(self):
        data = self.base / "clear"
        C.save_credentials(data, "synthetic-a", "synthetic-password")
        version = C.load_credentials(data)["version"]
        ready, release, clearing, cleared = (threading.Event() for _ in range(4))
        original = C._safe_write

        def paused(*args):
            ready.set()
            if not release.wait(10):
                raise TimeoutError("synthetic test synchronization")
            return original(*args)

        def clear():
            clearing.set()
            C.clear_credentials(data)
            cleared.set()

        first, second = {}, {}
        with patch.object(C, "_safe_write", side_effect=paused):
            writer = threading.Thread(target=_run_thread, args=(
                lambda: C.mark_login_failed(data, "synthetic", version), first))
            writer.start()
            self.assertTrue(ready.wait(5))
            clearer = threading.Thread(target=_run_thread, args=(clear, second))
            clearer.start()
            try:
                self.assertTrue(clearing.wait(5))
                cleared_inside_commit = cleared.wait(0.2)
            finally:
                release.set()
                writer.join(5)
                clearer.join(5)
        self.assertNotIn("error", first)
        self.assertNotIn("error", second)
        self.assertFalse(writer.is_alive() or clearer.is_alive())
        self.assertIsNone(C.load_credentials(data))
        self.assertFalse(cleared_inside_commit)
        self.assertFalse(C.mark_login_failed(data, "late old result", version))
        self.assertIsNone(C.load_credentials(data))

    def test_explicit_login_in_another_process_is_not_overwritten_by_old_publish(self):
        data = self.base / "publish"
        data.mkdir()
        acct_a = S.account_scope_id("synthetic-a", "synthetic-client")
        acct_b = S.account_scope_id("synthetic-b", "synthetic-client")
        S.save_session(data, acct_a, {"zjgsu_token": "synthetic-old", "created_at": "synthetic-old"})
        S.set_active(data, acct_a)
        generation = L.session_generation(data, acct_a)
        ready, release = threading.Event(), threading.Event()
        original = L.save_session

        def paused(*args):
            ready.set()
            if not release.wait(10):
                raise TimeoutError("synthetic test synchronization")
            return original(*args)

        result = {}
        with patch.object(L, "save_session", side_effect=paused):
            thread = threading.Thread(target=_run_thread, args=(
                lambda: _login(data, "synthetic-a", acct_a, generation), result))
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                signal = self.base / "login-signal"
                child = self._child("login", data, signal)
                completed_inside_commit = _wait_file(signal.with_suffix(".done"), 0.3)
            finally:
                release.set()
                thread.join(5)
            self._finish(child)
        self.assertNotIn("error", result)
        self.assertFalse(thread.is_alive())
        self.assertEqual(S.get_active_file(data), acct_b)
        self.assertFalse(completed_inside_commit)

    def test_cli_old_login_outcome_does_not_change_new_credential_version(self):
        from src.auth import login_cli as CLI
        for outcome in ("auth_failed", "ok"):
            with self.subTest(outcome=outcome):
                data = self.base / outcome
                C.save_credentials(data, "synthetic-a", "synthetic-old-password")

                def login(*args):
                    C.save_credentials(data, "synthetic-a", "synthetic-new-password")
                    if outcome == "ok":
                        C.mark_login_failed(data, "new version rejected independently")
                    return {"outcome": outcome, "diag": "synthetic"}

                with patch.object(CLI, "default_data_dir", return_value=data), \
                        patch.object(CLI, "login_and_save_session", side_effect=login), \
                        patch.object(CLI, "_print_login_result", return_value=0), \
                        contextlib.redirect_stdout(io.StringIO()):
                    CLI.cmd_autologin()
                self.assertEqual(C.load_credentials(data)["needs_update"], outcome == "ok")

    def test_same_second_session_writes_have_distinct_generations(self):
        data = self.base / "generation"
        acct = "synthetic-account"
        session = {"zjgsu_token": "synthetic-one", "created_at": "2026-10-19T12:00:00+08:00"}
        S.save_session(data, acct, session)
        first = L.session_generation(data, acct)
        S.save_session(data, acct, dict(session, zjgsu_token="synthetic-two"))
        self.assertNotEqual(L.session_generation(data, acct), first)

    def test_independent_import_does_not_write_after_logout_during_input(self):
        from src.auth import login_cli as CLI
        for command in (CLI.cmd_fitness_token, CLI.cmd_seat_cookie):
            with self.subTest(command=command.__name__):
                data = self.base / command.__name__
                acct = "synthetic-account"
                S.save_session(data, acct, {"zjgsu_token": "synthetic-app-token"})
                S.set_active(data, acct)

                def input_after_logout(*args):
                    S.delete_session(data, acct)
                    S.clear_active(data)
                    return "wechatSESS_ID=synthetic-token"

                with patch.dict(os.environ, {"ZJGSU_CAMPUS_DATA_DIR": str(data)}), \
                        patch.object(CLI.sys.stdin, "isatty", return_value=True), \
                        patch("getpass.getpass", side_effect=input_after_logout), \
                        patch("builtins.input", return_value="20990001"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = command()
                self.assertNotEqual(result, 0)
                self.assertFalse((data / "sessions" / (acct + ".json")).exists())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
    else:
        unittest.main()
