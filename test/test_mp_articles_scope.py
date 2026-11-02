# -*- coding: utf-8 -*-
"""公众号来源圈定与检索加速的合成测试（不触网、不读真实订阅库）。

覆盖：scope=all/school/school_confirmed、按 ID 的确认/排除、同名异 ID 不冒认、
普通 pending 订阅不混入、登记表损坏/无效/冲突/仅名称条目、mp_ids 交集与空数组、
名称变更、同义词与短词、未命中、count_total=false 的未知总数语义、分类与计时块。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.service.mp_articles_service import MpArticlesService
from test.helpers import temp_dir

NOW_TS = int(time.time())

FEEDS = [
    ("feed-main", "浙江工商大学"),
    ("feed-jwc", "浙江工商大学教务处"),
    ("feed-cs", "浙江工商大学计算机学院"),
    ("feed-outsider", "某科技观察"),
    ("feed-hz", "浙江工商大学杭州商学院"),
]
ARTICLES = [
    ("a1", "feed-main", "校庆公告", NOW_TS - 3600, "https://mp.weixin.qq.com/s/HASHa10000000000000000", "校内通知"),
    ("a2", "feed-jwc", "关于选课安排的通知", NOW_TS - 7200,
     "https://mp.weixin.qq.com/s/HASHa20000000000000000", "含体测时间说明"),
    ("a3", "feed-cs", "学院晨跑活动预告", NOW_TS - 86400,
     "https://mp.weixin.qq.com/s/HASHa30000000000000000", ""),
    ("a4", "feed-outsider", "行业观察：选课系统的产品逻辑", NOW_TS - 10800,
     "https://mp.weixin.qq.com/s/HASHa40000000000000000", ""),
    ("a5", "feed-hz", "独立学院招生简章", NOW_TS - 9000,
     "https://mp.weixin.qq.com/s/HASHa50000000000000000", ""),
]


def build_db(path: Path, feeds=FEEDS, articles=ARTICLES):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE feeds (id TEXT PRIMARY KEY, mp_name TEXT)")
    conn.execute("""CREATE TABLE articles (
        id TEXT PRIMARY KEY, mp_id TEXT, title TEXT, pic_url TEXT,
        url TEXT, description TEXT, publish_time INTEGER,
        created_at DATETIME, updated_at BIGINT, status INTEGER)""")
    conn.executemany("INSERT INTO feeds VALUES (?,?)", feeds)
    for aid, mpid, title, ts, url, desc in articles:
        conn.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (aid, mpid, title, "", url, desc, ts, "2026-10-28 09:00:00", NOW_TS, 1))
    conn.commit()
    conn.close()


def write_registry(path: Path, sources):
    path.write_text(json.dumps({"version": 1, "sources": sources}, ensure_ascii=False),
                    encoding="utf-8")


class ScopeTests:
    """夹具混入：临时库 + 可选登记表。"""

    def _setup(self, registry_sources=None):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "db.db"
        build_db(self.db)
        self.registry = Path(self._tmp.name) / "mp_sources.json"
        if registry_sources is not None:
            write_registry(self.registry, registry_sources)
        self.svc = MpArticlesService(db_path=self.db, sources_path=self.registry)


def _add_feed(db: Path, feed_id: str, name: str, article_id: str | None = None):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO feeds VALUES (?,?)", (feed_id, name))
    if article_id:
        conn.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (article_id, feed_id, "同名号推送%s" % article_id, "",
                      "https://mp.weixin.qq.com/s/%s" % ("CLONEHASH" + article_id).ljust(22, "0")[:22], "",
                      NOW_TS - 600, "2026-10-28 09:00:00", NOW_TS, 1))
    conn.commit()
    conn.close()


class MpArticlesScopeTests(ScopeTests, unittest.TestCase):
    def titles(self, result):
        return [item["title"] for item in result["data"]["articles"]]

    def test_blob_article_url_is_type_unsupported_not_misclassified_as_unsafe(self):
        self._setup()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE articles SET url=? WHERE id=?",
                     (sqlite3.Binary(b"synthetic-private-url-value"), "a1"))
        conn.commit()
        conn.close()
        result = self.svc.get_mp_articles(days=0, limit=50)
        article = next(item for item in result["data"]["articles"] if item["title"] == "校庆公告")
        self.assertIsNone(article["url"])
        self.assertEqual(article["url_status"], "type_unsupported")
        self.assertIsNotNone(article["search_hint"])
        codes = {item["code"] for item in result["issues"]}
        self.assertIn("url_type_unsupported", codes)
        self.assertNotIn("unsafe_url_omitted", codes)
        self.assertNotIn("synthetic-private-url-value", json.dumps(result, ensure_ascii=False))

    def test_stale_registry_ids_are_counted_without_exposing_them(self):
        self._setup([{"id": "synthetic-missing-private", "status": "excluded"},
                     {"id": "feed-jwc", "status": "confirmed"}])
        result = self.svc.get_mp_articles(days=0, scope="school")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope_info"]["registry"]["not_in_local_feeds"], 1)
        issues = {item["code"]: item["safe_message"] for item in result["issues"]}
        self.assertIn("registry_id_not_in_local_feeds", issues)
        self.assertIn("不代表", issues["registry_id_not_in_local_feeds"])
        self.assertNotIn("synthetic-missing-private", json.dumps(result, ensure_ascii=False))

    def test_valid_registry_does_not_report_stale_ids(self):
        self._setup([{"id": "feed-hz", "status": "excluded"}])
        result = self.svc.get_mp_articles(days=0, scope="school")
        self.assertEqual(result["scope_info"]["registry"]["not_in_local_feeds"], 0)
        self.assertNotIn("registry_id_not_in_local_feeds", [i["code"] for i in result["issues"]])

    def test_database_path_that_is_directory_has_specific_safe_reason(self):
        self._setup()
        result = MpArticlesService(db_path=self.db.parent, sources_path=self.registry).get_mp_articles()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("目录", result["reason"])
        self.assertNotIn(str(self.db.parent), json.dumps(result, ensure_ascii=False))

    def test_missing_source_unknown_status_and_time_are_not_silently_lost(self):
        self._setup()
        conn = sqlite3.connect(self.db)
        for aid, mpid, status, ts in [
            ("orphan-null", None, 1, NOW_TS),
            ("orphan-space", "feed-jwc ", 1, NOW_TS),
            ("unknown-status", "feed-jwc", None, NOW_TS),
            ("unknown-time", "feed-jwc", 1, "not-a-timestamp"),
        ]:
            conn.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)", (
                aid, mpid, aid, "", "", "", ts, "", "", status))
        conn.commit()
        conn.close()
        whole = self.svc.get_mp_articles(days=0, limit=50)
        self.assertEqual(whole["status"], "partial")
        articles = {a["title"]: a for a in whole["data"]["articles"]}
        self.assertEqual(articles["orphan-null"]["source_status"], "unknown")
        self.assertEqual(articles["orphan-space"]["source_status"], "unknown")
        self.assertFalse(articles["unknown-time"]["publish_time_known"])
        self.assertNotIn("unknown-status", articles)
        self.assertEqual(whole["coverage"]["total_matched"], 8)
        codes = {i["code"] for i in whole["issues"]}
        self.assertTrue({"unmatched_article_source", "unknown_article_status"} <= codes)
        window = self.svc.get_mp_articles(days=7, limit=50, scope="school")
        self.assertEqual(window["status"], "partial")
        self.assertNotIn("unknown-time", self.titles(window))
        codes = {i["code"] for i in window["issues"]}
        self.assertTrue({"unmatched_article_source", "unknown_article_status",
                         "unknown_article_time"} <= codes)
        self.assertTrue(any("整个" in i["safe_message"] for i in window["issues"]
                            if i["code"] == "unknown_article_time"))

    def test_all_scope_ids_count_and_all_unmatched_are_reported(self):
        self._setup()
        missing = ["synthetic-missing-%02d" % i for i in range(12)]
        result = self.svc.get_mp_articles(days=0, mp_ids=["feed-main", *missing])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope_info"]["selected_sources"]["matched"], 1)
        self.assertIsNone(result["scope_info"]["selected_sources"]["confirmed"])
        self.assertEqual(result["scope_info"]["unmatched_mp_ids"], missing)

    def test_out_of_range_timestamp_is_unknown_but_a_future_time_remains_valid(self):
        self._setup()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE articles SET publish_time=? WHERE id='a1'", (1e30,))
        conn.execute("UPDATE articles SET publish_time=? WHERE id='a2'", (NOW_TS + 86400 * 365,))
        conn.commit()
        conn.close()
        all_time = self.svc.get_mp_articles(days=0, limit=50)
        articles = {a["title"]: a for a in all_time["data"]["articles"]}
        self.assertFalse(articles["校庆公告"]["publish_time_known"])
        self.assertTrue(articles["关于选课安排的通知"]["publish_time_known"])
        window = self.svc.get_mp_articles(days=7)
        self.assertEqual(window["status"], "partial")
        self.assertIn("unknown_article_time", [i["code"] for i in window["issues"]])
        self.assertNotIn("校庆公告", self.titles(window))
        self.assertNotIn("关于选课安排的通知", self.titles(window))

    def test_database_parameter_limit_error_has_specific_safe_reason(self):
        self._setup()
        real_connection = self.svc._connect()
        real_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 5)
        with patch.object(self.svc, "_connect", return_value=real_connection):
            result = self.svc.get_mp_articles(scope="school", days=0,
                                              keywords=["甲", "乙", "丙"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("参数上限", result["reason"])
        self.assertNotIn(str(self.db), json.dumps(result, ensure_ascii=False))

    def test_data_directory_isolates_default_registry_and_explicit_overrides_win(self):
        self._setup()
        profile_a = self.db.parent / "profile-a"
        profile_b = self.db.parent / "profile-b"
        profile_a.mkdir()
        profile_b.mkdir()
        reg_a = profile_a / "mp_sources.json"
        reg_b = profile_b / "mp_sources.json"
        write_registry(reg_a, [{"id": "feed-jwc", "status": "confirmed"}])
        write_registry(reg_b, [{"id": "feed-cs", "status": "confirmed"},
                               {"id": "feed-jwc", "status": "excluded"}])
        base_env = dict(os.environ)
        base_env.pop("ZJGSU_MP_SOURCES", None)
        for profile, expected in ((profile_a, "feed-jwc"), (profile_b, "feed-cs")):
            env = dict(base_env, ZJGSU_CAMPUS_DATA_DIR=str(profile))
            with self.subTest(profile=profile.name), patch.dict(os.environ, env, clear=True):
                result = MpArticlesService(db_path=self.db).get_mp_articles(scope="school_confirmed", days=0)
                self.assertEqual(result["status"], "ok")
                self.assertEqual([item["mp_id"] for item in result["data"]["articles"]], [expected])
        env = dict(base_env, ZJGSU_CAMPUS_DATA_DIR=str(profile_b), ZJGSU_MP_SOURCES=str(reg_a))
        with patch.dict(os.environ, env, clear=True):
            env_result = MpArticlesService(db_path=self.db).get_mp_articles(scope="school_confirmed", days=0)
            explicit_result = MpArticlesService(db_path=self.db, sources_path=reg_b).get_mp_articles(scope="school_confirmed", days=0)
            self.assertEqual([item["mp_id"] for item in env_result["data"]["articles"]], ["feed-jwc"])
            self.assertEqual([item["mp_id"] for item in explicit_result["data"]["articles"]], ["feed-cs"])

    def test_registry_replacement_with_unchanged_size_and_mtime_revokes_confirmation(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处", "status": "confirmed"}])
        first = self.svc.get_mp_articles(scope="school_confirmed", days=0)
        self.assertEqual([item["mp_id"] for item in first["data"]["articles"]], ["feed-jwc"])
        before = self.registry.stat()
        old = self.registry.read_text(encoding="utf-8")
        changed = old.replace('"confirmed"', '"excluded "')
        self.assertNotEqual(old, changed)
        self.assertEqual(len(old.encode("utf-8")), len(changed.encode("utf-8")))
        self.registry.write_text(changed, encoding="utf-8")
        os.utime(self.registry, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.registry.stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        result = self.svc.get_mp_articles(scope="school_confirmed", days=0)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)
        school = self.svc.get_mp_articles(scope="school", days=0, limit=50)
        self.assertNotIn("feed-jwc", [item["mp_id"] for item in school["data"]["articles"]])

    def test_all_scope_keeps_previous_behaviour(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, limit=50)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"]["total_matched"], 5)
        self.assertEqual(result["scope_info"]["scope"], "all")
        self.assertTrue(all(item["source_status"] == "unscoped"
                            for item in result["data"]["articles"]))

    def test_school_scope_drops_outsiders_and_excluded_sources(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处",
                      "status": "confirmed", "category": "教务"},
                     {"id": "feed-hz", "name": "浙江工商大学杭州商学院",
                      "status": "excluded", "note": "独立学院，非本校主体"}])
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        self.assertEqual(result["status"], "ok")
        names = {item["mp_name"] for item in result["data"]["articles"]}
        self.assertEqual(names, {"浙江工商大学", "浙江工商大学教务处", "浙江工商大学计算机学院"})
        info = result["scope_info"]
        self.assertEqual(info["scope_sources"]["confirmed"], 1)
        self.assertEqual(info["scope_sources"]["pending"], 2)
        self.assertEqual(info["scope_sources"]["excluded"], 1)
        self.assertEqual(info["scope_sources"]["unclassified"], 1)  # 非学校名称、无证据
        self.assertEqual(info["selected_sources"]["matched"], 3)
        self.assertTrue(info["registry"]["configured"])

    def test_school_confirmed_scope_only_uses_registry_confirmed(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处",
                      "status": "confirmed", "category": "教务"}])
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school_confirmed")
        self.assertEqual(result["status"], "ok")
        self.assertEqual({item["mp_id"] for item in result["data"]["articles"]}, {"feed-jwc"})
        self.assertEqual(result["scope_info"]["scope_sources"]["pending"], 0)
        self.assertEqual(result["scope_info"]["selected_sources"]["confirmed"], 1)
        self.assertTrue(all(item["source_status"] == "confirmed"
                            for item in result["data"]["articles"]))

    def test_school_confirmed_without_registry_is_unavailable_not_widened(self):
        self._setup()  # 未配置登记表
        result = self.svc.get_mp_articles(days=0, scope="school_confirmed")
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("学校来源未解析", result["reason"])
        self.assertNotIn("data", result)  # 不得退回全部订阅
        self.assertFalse(result["scope_info"]["registry"]["configured"])

    def test_school_scope_without_registry_still_candidates_but_flags_pending(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope_info"]["scope_sources"]["confirmed"], 0)
        self.assertEqual(result["scope_info"]["scope_sources"]["pending"], 4)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("school_scope_candidates", codes)
        self.assertIn("registry_missing", codes)
        self.assertTrue(any("school_evidence" in gap for gap in result["gaps"]))

    # ---- 反馈 §2.174 反例 1：同名异 ID 不得冒认 ----

    def test_same_name_different_id_never_inherits_confirmed(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处",
                      "status": "confirmed", "category": "教务"}])
        _add_feed(self.db, "feed-jwc-clone", "浙江工商大学教务处", "a6")

        confirmed = self.svc.get_mp_articles(days=0, limit=50, scope="school_confirmed")
        self.assertEqual({i["mp_id"] for i in confirmed["data"]["articles"]}, {"feed-jwc"})

        school = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        statuses = {i["mp_id"]: i["source_status"] for i in school["data"]["articles"]}
        self.assertEqual(statuses["feed-jwc"], "confirmed")
        self.assertEqual(statuses["feed-jwc-clone"], "pending")  # 候选，不是确认

    def test_same_name_different_id_never_inherits_excluded(self):
        self._setup([{"id": "feed-hz", "name": "浙江工商大学杭州商学院",
                      "status": "excluded"}])
        _add_feed(self.db, "feed-hz2", "浙江工商大学杭州商学院", "a6")
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        statuses = {i["mp_id"]: i["source_status"] for i in result["data"]["articles"]}
        self.assertNotIn("feed-hz", statuses)          # 被 ID 排除
        self.assertEqual(statuses["feed-hz2"], "pending")  # 同名不同 ID 不被连带排除

    # ---- 反馈 §2.174 反例 2：空 ID 列表不得扩成全订阅 ----

    def test_empty_mp_ids_is_rejected_in_every_scope(self):
        self._setup()
        for scope in ("all", "school", "school_confirmed"):
            with self.subTest(scope=scope):
                result = self.svc.get_mp_articles(days=0, scope=scope, mp_ids=[])
                self.assertEqual(result["status"], "unavailable")
                self.assertIn("不能为空", result["reason"])
                self.assertNotIn("data", result)
        for value in ("", " , "):
            self.assertEqual(self.svc.get_mp_articles(mp_ids=value)["status"], "unavailable")

    # ---- 反馈 §2.174 反例 3：普通 pending 订阅不得混入学校范围 ----

    def test_plain_pending_subscription_stays_out_of_school_scope(self):
        self._setup([{"id": "feed-outsider", "name": "某科技观察", "status": "pending"}])
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        self.assertNotIn("某科技观察", {i["mp_name"] for i in result["data"]["articles"]})
        self.assertEqual(result["scope_info"]["scope_sources"]["unclassified"], 1)

    def test_pending_with_school_evidence_enters_as_candidate(self):
        self._setup([{"id": "feed-outsider", "name": "某科技观察", "status": "pending",
                      "school_evidence": "校方公告引用其稿件"}])
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        statuses = {i["mp_name"]: i["source_status"] for i in result["data"]["articles"]}
        self.assertEqual(statuses["某科技观察"], "pending")

    # ---- 反馈 §2.174：登记表损坏/无效/冲突/仅名称/名称变更 ----

    def test_corrupt_registry_refuses_school_scopes_instead_of_dropping_exclusions(self):
        self._setup()
        self.registry.write_text("{not json", encoding="utf-8")
        for scope in ("school", "school_confirmed"):
            with self.subTest(scope=scope):
                result = self.svc.get_mp_articles(days=0, scope=scope)
                self.assertEqual(result["status"], "unavailable")
                self.assertIn("无法保证排除规则", result["reason"])
                self.assertEqual(result["scope_info"]["registry"]["error"], "registry_unreadable")
        self.assertEqual(self.svc.get_mp_articles(days=0)["status"], "ok")

    def test_invalid_entries_cannot_silently_disable_school_exclusions(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处", "status": "confirmed"},
                     {"id": "feed-hz", "name": "浙江工商大学杭州商学院", "status": "excluded", "aliases": 42}])
        for scope in ("school", "school_confirmed"):
            with self.subTest(scope=scope):
                result = self.svc.get_mp_articles(days=0, limit=50, scope=scope)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("data", result)
                self.assertEqual(result["scope_info"]["registry"]["invalid_entries"], 1)
                self.assertIn("registry_partial", {issue["code"] for issue in result["issues"]})
        self.assertEqual(self.svc.get_mp_articles(days=0, scope="all")["status"], "ok")

    def test_exclusions_without_valid_id_and_nonstring_ids_fail_school_scopes(self):
        self._setup()
        records = [
            {"id": 123, "name": "浙江工商大学杭州商学院", "status": "excluded"},
            {"id": None, "name": "浙江工商大学杭州商学院", "status": "pending"},
            {"name": "浙江工商大学杭州商学院", "status": "excluded"},
            {"id": "   ", "name": "浙江工商大学杭州商学院", "status": "excluded"},
        ]
        for record in records:
            write_registry(self.registry, [record])
            for scope in ("school", "school_confirmed"):
                with self.subTest(record=record, scope=scope):
                    result = self.svc.get_mp_articles(days=0, scope=scope)
                    self.assertEqual(result["status"], "unavailable")
                    self.assertNotIn("data", result)
                    self.assertEqual(result["scope_info"]["registry"]["invalid_entries"], 1)
                    self.assertIn("registry_partial", {issue["code"] for issue in result["issues"]})
            self.assertEqual(self.svc.get_mp_articles(days=0, scope="all")["status"], "ok")

    def test_registry_stat_errors_do_not_discard_school_exclusions(self):
        self._setup([{"id": "feed-hz", "name": "浙江工商大学杭州商学院", "status": "excluded"}])
        real_stat = Path.stat
        for error in (PermissionError("synthetic permission"), OSError("synthetic io")):
            def stat(path, *args, **kwargs):
                if path == self.registry:
                    raise error
                return real_stat(path, *args, **kwargs)
            with self.subTest(error=type(error).__name__), patch.object(Path, "stat", autospec=True, side_effect=stat):
                for scope in ("school", "school_confirmed"):
                    result = self.svc.get_mp_articles(days=0, scope=scope)
                    self.assertEqual(result["status"], "unavailable")
                    self.assertNotIn("data", result)
                    self.assertEqual(result["scope_info"]["registry"]["error"], "registry_unreadable")
                self.assertEqual(self.svc.get_mp_articles(days=0, scope="all")["status"], "ok")

    def test_conflicting_duplicate_ids_get_no_authority(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处", "status": "confirmed"},
                     {"id": "feed-jwc", "name": "浙江工商大学教务处", "status": "excluded"}])
        confirmed = self.svc.get_mp_articles(days=0, limit=50, scope="school_confirmed")
        self.assertEqual(confirmed["status"], "unavailable")
        self.assertNotIn("data", confirmed)  # 冲突项不授予确认 → 无来源
        self.assertIn("registry_conflict", {issue["code"] for issue in confirmed["issues"]})
        school = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        self.assertEqual(school["status"], "unavailable")
        self.assertNotIn("data", school)
        self.assertIn("registry_conflict", {issue["code"] for issue in school["issues"]})
        self.assertEqual(self.svc.get_mp_articles(days=0, scope="all")["status"], "ok")

    def test_confirmed_survives_name_change_because_identity_is_id(self):
        self._setup([{"id": "feed-jwc", "name": "旧名称教务处", "status": "confirmed"}])
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE feeds SET mp_name=? WHERE id='feed-jwc'", ("浙江工商大学教务处",))
        conn.commit()
        conn.close()
        result = self.svc.get_mp_articles(days=0, scope="school_confirmed")
        self.assertEqual({i["mp_id"] for i in result["data"]["articles"]}, {"feed-jwc"})

    def test_name_only_entry_never_confirms_or_excludes(self):
        self._setup([{"name": "浙江工商大学教务处", "status": "confirmed"}])
        confirmed = self.svc.get_mp_articles(days=0, scope="school_confirmed")
        self.assertEqual(confirmed["status"], "unavailable")
        school = self.svc.get_mp_articles(days=0, limit=50, scope="school")
        statuses = {i["mp_id"]: i["source_status"] for i in school["data"]["articles"]}
        self.assertEqual(statuses["feed-jwc"], "pending")

    # ---- 范围口径、分类与计时 ----

    def test_scope_and_selected_counts_are_reported_separately(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处", "status": "confirmed"}])
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school",
                                         mp_ids=["feed-cs"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope_info"]["scope_sources"]["confirmed"], 1)
        self.assertEqual(result["scope_info"]["selected_sources"],
                         {"matched": 1, "confirmed": 0, "pending": 1})
        self.assertEqual([i["mp_id"] for i in result["data"]["articles"]], ["feed-cs"])
        self.assertEqual(result["data"]["articles"][0]["source_status"], "pending")

    def test_mp_ids_intersect_with_school_scope_and_report_unmatched(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, limit=50, scope="school",
                                         mp_ids=["feed-cs", "feed-outsider", "feed-missing"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual({item["mp_id"] for item in result["data"]["articles"]}, {"feed-cs"})
        self.assertEqual(sorted(result["scope_info"]["unmatched_mp_ids"]),
                         ["feed-missing", "feed-outsider"])

    def test_mp_ids_in_all_scope_are_checked_against_subscription_table(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, scope="all",
                                         mp_ids=["feed-outsider", "feed-missing"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.titles(result), ["行业观察：选课系统的产品逻辑"])
        self.assertEqual(result["scope_info"]["id_check"], "checked")
        self.assertEqual(result["scope_info"]["unmatched_mp_ids"], ["feed-missing"])

    def test_single_id_hit_and_all_miss(self):
        self._setup()
        hit = self.svc.get_mp_articles(days=0, scope="all", mp_ids=["feed-cs"])
        self.assertEqual(len(hit["data"]["articles"]), 1)
        miss = self.svc.get_mp_articles(days=0, scope="all", mp_ids=["feed-none"])
        self.assertEqual(miss["status"], "ok")
        self.assertEqual(miss["coverage"]["total_matched"], 0)
        self.assertEqual(miss["scope_info"]["unmatched_mp_ids"], ["feed-none"])

    def test_keywords_or_group_matches_short_words(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, limit=50, keywords=["体测", "晨跑"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(self.titles(result)),
                         {"关于选课安排的通知", "学院晨跑活动预告"})

    def test_keyword_and_keywords_are_unioned_and_deduplicated(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, limit=50, keyword="选课",
                                         keywords=["选课", "晨跑"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(self.titles(result)),
                         {"关于选课安排的通知", "学院晨跑活动预告",
                          "行业观察：选课系统的产品逻辑"})

    def test_keyword_no_match_is_empty_ok_not_error(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, keywords=["不存在的主题词"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"]["total_matched"], 0)
        self.assertEqual(result["data"]["articles"], [])

    def test_keyword_terms_are_bounded(self):
        self._setup()
        result = self.svc.get_mp_articles(keywords=["词%d" % i for i in range(13)])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("最多 12 个", result["reason"])

    def test_invalid_scope_ids_and_count_total_are_rejected(self):
        self._setup()
        self.assertEqual(self.svc.get_mp_articles(scope="campus")["status"], "unavailable")
        self.assertEqual(self.svc.get_mp_articles(mp_ids=[123])["status"], "unavailable")
        self.assertEqual(self.svc.get_mp_articles(mp_ids=["x"] * 51)["status"], "unavailable")
        self.assertEqual(self.svc.get_mp_articles(count_total="yes")["status"], "unavailable")
        self.assertEqual(self.svc.get_mp_articles(keywords="体测")["status"], "unavailable")

    def test_count_total_false_reports_unknown_total_and_keeps_paging(self):
        self._setup()
        first = self.svc.get_mp_articles(days=0, limit=2, count_total=False)
        self.assertEqual(first["status"], "ok")
        self.assertIsNone(first["coverage"]["total_matched"])
        self.assertFalse(first["coverage"]["total_matched_known"])
        self.assertTrue(first["pagination"]["has_more"])
        self.assertEqual(len(first["data"]["articles"]), 2)
        codes = {issue["code"] for issue in first["issues"]}
        self.assertIn("total_unknown", codes)

        seen = [item["title"] for item in first["data"]["articles"]]
        cursor = first["pagination"]["next_offset"]
        while cursor is not None:
            page = self.svc.get_mp_articles(days=0, limit=2, offset=cursor,
                                            count_total=False)
            seen.extend(item["title"] for item in page["data"]["articles"])
            cursor = page["pagination"]["next_offset"]
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_count_total_false_last_page_has_no_more(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, limit=5, count_total=False)
        self.assertFalse(result["pagination"]["has_more"])
        self.assertIsNone(result["pagination"]["next_offset"])
        self.assertEqual(len(result["data"]["articles"]), 5)

    def test_source_category_is_rule_based_and_present_for_all_scopes(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, limit=50, scope="all")
        mapping = {item["mp_name"]: item["source_category"] for item in result["data"]["articles"]}
        self.assertEqual(mapping["浙江工商大学教务处"], "教务")
        self.assertEqual(mapping["浙江工商大学计算机学院"], "学院")
        self.assertEqual(mapping["某科技观察"], "其他")
        self.assertEqual(mapping["浙江工商大学"], "校级")

    def test_registry_category_overrides_name_rule(self):
        self._setup([{"id": "feed-jwc", "name": "浙江工商大学教务处",
                      "status": "confirmed", "category": "教学管理"}])
        result = self.svc.get_mp_articles(days=0, scope="school_confirmed")
        self.assertEqual(result["data"]["articles"][0]["source_category"], "教学管理")

    def test_performance_block_is_present_and_leaks_no_path(self):
        self._setup()
        result = self.svc.get_mp_articles(days=0, scope="school")
        perf = result["performance"]["local_db"]
        for key in ("total_ms", "connect_ms", "schema_check_ms", "scope_resolve_ms",
                    "query_ms", "postprocess_ms", "sql_statements", "mode"):
            self.assertIn(key, perf)
        self.assertEqual(perf["mode"], "live_local_db")
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.db), text)
        self.assertNotIn(str(self.registry), text)

    def test_schema_check_is_cached_for_unchanged_database(self):
        self._setup()
        first = self.svc.get_mp_articles(days=0)
        second = self.svc.get_mp_articles(days=0)
        self.assertGreater(first["performance"]["local_db"]["schema_check_ms"], 0)
        self.assertEqual(second["performance"]["local_db"]["schema_check_ms"], 0)
        self.assertEqual(first["coverage"]["total_matched"], second["coverage"]["total_matched"])

    def test_scope_filter_does_not_change_pagination_semantics(self):
        self._setup()
        first = self.svc.get_mp_articles(days=0, limit=2, scope="school")
        self.assertTrue(first["pagination"]["has_more"])
        second = self.svc.get_mp_articles(days=0, limit=2, scope="school",
                                         offset=first["pagination"]["next_offset"])
        first_titles = set(self.titles(first))
        second_titles = set(self.titles(second))
        self.assertFalse(first_titles & second_titles)
        self.assertEqual(len(first_titles | second_titles), 4)


if __name__ == "__main__":
    unittest.main()
