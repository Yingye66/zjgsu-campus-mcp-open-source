# -*- coding: utf-8 -*-
"""F29 凭据存储：读写/时效/熔断/损坏容错/掩码输出（不触网）。"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from test.helpers import temp_dir

from src.auth import credential_store as C


class CredentialStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # ---------- 基本读写 ----------
    def test_save_load_roundtrip_and_masked_status(self):
        # 合成测试身份（禁止使用真实学号）
        C.save_credentials(self.data_dir, "20990001", "pw-secret", ttl_days=30)
        cred = C.load_credentials(self.data_dir)
        self.assertIsNotNone(cred)
        self.assertEqual(cred["username"], "20990001")
        self.assertEqual(cred["password"], "pw-secret")
        self.assertEqual(cred["ttl_days"], 30)
        self.assertEqual(cred["schema"], C.SCHEMA)
        self.assertTrue(cred["version"])

        st = C.status_for_display(self.data_dir)
        self.assertTrue(st["present"])
        self.assertEqual(st["username_masked"], "2099****01")
        # 状态输出绝不含密码原文（掩码纪律）
        self.assertNotIn("pw-secret", json.dumps(st, ensure_ascii=False))

    def test_save_overwrite_resets_fail_state(self):
        C.save_credentials(self.data_dir, "user1", "pw-old")
        C.mark_login_failed(self.data_dir, "旧密码错误")
        self.assertFalse(C.evaluate(C.load_credentials(self.data_dir))["usable"])
        # 重新保存 = 更新凭据 → 熔断解除
        C.save_credentials(self.data_dir, "user1", "pw-new")
        ev = C.evaluate(C.load_credentials(self.data_dir))
        self.assertTrue(ev["usable"])
        self.assertEqual(ev["reason"], C.REASON_OK)

    def test_empty_values_rejected(self):
        with self.assertRaises(ValueError):
            C.save_credentials(self.data_dir, "", "x")
        with self.assertRaises(ValueError):
            C.save_credentials(self.data_dir, "u", "")
        with self.assertRaises(ValueError):
            C.save_credentials(self.data_dir, "u", "x", ttl_days=0)
        with self.assertRaises(ValueError):
            C.save_credentials(self.data_dir, "u", "x", ttl_days=-5)
        self.assertIsNone(C.load_credentials(self.data_dir))

    def test_non_int_ttl_falls_back_to_default(self):
        C.save_credentials(self.data_dir, "user1", "pw", ttl_days="abc")
        cred = C.load_credentials(self.data_dir)
        self.assertEqual(cred["ttl_days"], C.DEFAULT_TTL_DAYS)

    # ---------- 时效判定 ----------
    def test_expired_when_past_ttl(self):
        C.save_credentials(self.data_dir, "user1", "pw", ttl_days=10)
        cred = C.load_credentials(self.data_dir)
        saved = datetime.fromisoformat(cred["saved_at"])
        ev_late = C.evaluate(cred, now=saved + timedelta(days=11))
        self.assertFalse(ev_late["usable"])
        self.assertEqual(ev_late["reason"], C.REASON_EXPIRED)
        self.assertTrue(ev_late["expired"])
        ev_ok = C.evaluate(cred, now=saved + timedelta(days=9))
        self.assertTrue(ev_ok["usable"])
        self.assertEqual(ev_ok["reason"], C.REASON_OK)

    def test_ttl_boundary_is_exclusive(self):
        C.save_credentials(self.data_dir, "user1", "pw", ttl_days=10)
        cred = C.load_credentials(self.data_dir)
        saved = datetime.fromisoformat(cred["saved_at"])
        # 恰好等于 TTL 不算超期（> 才算）
        self.assertTrue(C.evaluate(cred, now=saved + timedelta(days=10))["usable"])
        self.assertFalse(C.evaluate(cred, now=saved + timedelta(days=10, seconds=1))["usable"])

    # ---------- 损坏/缺失容错 ----------
    def test_corrupt_file_treated_as_missing(self):
        (self.data_dir / C.FILENAME).write_text("{not json", encoding="utf-8")
        self.assertIsNone(C.load_credentials(self.data_dir))
        st = C.status_for_display(self.data_dir)
        self.assertFalse(st["present"])
        self.assertEqual(st["reason"], C.REASON_MISSING)

    def test_schema_mismatch_rejected(self):
        (self.data_dir / C.FILENAME).write_text(
            json.dumps({"schema": 99, "username": "u", "password": "p"}),
            encoding="utf-8")
        self.assertIsNone(C.load_credentials(self.data_dir))

    def test_missing_fields_rejected(self):
        (self.data_dir / C.FILENAME).write_text(
            json.dumps({"schema": 1, "username": "u"}),
            encoding="utf-8")
        self.assertIsNone(C.load_credentials(self.data_dir))

    # ---------- 熔断状态 ----------
    def test_failed_marks_needs_update_then_ok_resets(self):
        C.save_credentials(self.data_dir, "user1", "pw", ttl_days=30)
        C.mark_login_failed(self.data_dir, "auth_failed：密码错误")
        cred = C.load_credentials(self.data_dir)
        ev = C.evaluate(cred)
        self.assertFalse(ev["usable"])
        self.assertEqual(ev["reason"], C.REASON_NEEDS_UPDATE)
        self.assertEqual(cred["fail_count"], 1)
        self.assertEqual(cred["last_fail_diag"], "auth_failed：密码错误")

        C.mark_login_failed(self.data_dir, "again")  # 累计计数
        self.assertEqual(C.load_credentials(self.data_dir)["fail_count"], 2)

        C.mark_login_ok(self.data_dir)  # 成功重置
        cred = C.load_credentials(self.data_dir)
        self.assertTrue(C.evaluate(cred)["usable"])
        self.assertEqual(cred["fail_count"], 0)
        self.assertIsNone(cred["last_fail_diag"])
        self.assertIsNotNone(cred["last_login_ok_at"])

    def test_mark_on_missing_credentials_is_noop(self):
        C.mark_login_ok(self.data_dir)      # 不抛异常
        C.mark_login_failed(self.data_dir, "x")
        self.assertIsNone(C.load_credentials(self.data_dir))

    # ---------- 清除 ----------
    def test_clear(self):
        C.save_credentials(self.data_dir, "user1", "pw")
        self.assertTrue(C.clear_credentials(self.data_dir))
        self.assertFalse(C.clear_credentials(self.data_dir))
        self.assertIsNone(C.load_credentials(self.data_dir))

    # ---------- 原子写卫生 ----------
    def test_no_tmp_leftovers(self):
        C.save_credentials(self.data_dir, "user1", "pw")
        C.mark_login_failed(self.data_dir, "x")
        C.mark_login_ok(self.data_dir)
        leftovers = [p.name for p in self.data_dir.iterdir()
                     if p.name.startswith(".cred-")]
        self.assertEqual(leftovers, [])

    # ---------- F29 审查修复：版本与元数据 ----------
    def test_version_changes_on_every_save(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        v1 = C.load_credentials(self.data_dir)["version"]
        # 同秒、等长改密：版本必须不同（不能用长度/秒级时间识别）
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass2")
        v2 = C.load_credentials(self.data_dir)["version"]
        self.assertNotEqual(v1, v2)

    def test_invalid_metadata_treated_as_unusable(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        valid = C.load_credentials(self.data_dir)
        cases = [
            {"saved_at": None},
            {"saved_at": "not-a-date"},
            {"saved_at": "2026-10-19T12:00:00"},   # 无时区（naive）
            {"ttl_days": "not-a-number"},
            {"ttl_days": 0},
            {"ttl_days": float("nan")},
            {"ttl_days": True},
            {"version": ""},
        ]
        for updates in cases:
            cred = dict(valid, **updates)
            (self.data_dir / C.FILENAME).write_text(json.dumps(cred), encoding="utf-8")
            self.assertIsNone(C.load_credentials(self.data_dir), updates)
        # 时效元数据非法时，直接 evaluate 也不得 usable、不得抛异常
        # （version 仅由 load 层校验，evaluate 不参与账号/版本判定）
        for updates in ({"saved_at": None}, {"saved_at": "not-a-date"},
                        {"saved_at": "2026-10-19T12:00:00"},
                        {"ttl_days": "not-a-number"}, {"ttl_days": 0},
                        {"ttl_days": float("nan")}, {"ttl_days": True}):
            ev = C.evaluate({"saved_at": updates.get("saved_at", valid["saved_at"]),
                             "ttl_days": updates.get("ttl_days", valid["ttl_days"])})
            self.assertFalse(ev["usable"], updates)

    def test_mark_persist_failure_is_controlled(self):
        C.save_credentials(self.data_dir, "synthetic-user", "synthetic-pass")
        with mock.patch.object(C, "_write_atomic", side_effect=OSError("disk")):
            self.assertFalse(C.mark_login_failed(self.data_dir, "x"))  # 不抛
            self.assertFalse(C.mark_login_ok(self.data_dir))


if __name__ == "__main__":
    unittest.main()
