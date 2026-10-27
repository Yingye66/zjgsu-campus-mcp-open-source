# -*- coding: utf-8 -*-
"""公众号文章查询服务测试（合成数据、不触网）：
正常查询/公众号过滤/关键词/时间窗/limit 边界/库缺失/表结构不匹配/时间戳转换。

数据源为本机 we-mp-rss 采集库（只读），测试用临时库 + 环境变量隔离。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.service.mp_articles_service import MpArticlesService

NOW_TS = int(time.time())


def build_db(path: Path):
    """构造与 we-mp-rss 相同口径的最小库结构。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE feeds (id TEXT PRIMARY KEY, mp_name TEXT)")
    conn.execute("""CREATE TABLE articles (
        id TEXT PRIMARY KEY, mp_id TEXT, title TEXT, pic_url TEXT,
        url TEXT, description TEXT, publish_time INTEGER,
        created_at DATETIME, updated_at BIGINT, status INTEGER)""")
    feeds = [
        ("synthetic-feed-a", "示例大学"),
        ("synthetic-feed-b", "示例大学教务处"),
        ("synthetic-feed-c", "示例大学计算机学院"),
    ]
    conn.executemany("INSERT INTO feeds VALUES (?,?)", feeds)
    arts = [
        ("a1", "synthetic-feed-a", "官微最新推送", NOW_TS - 3600,
         "https://mp.weixin.qq.com/s/a1", "摘要一"),
        ("a2", "synthetic-feed-b", "关于考试安排的通知", NOW_TS - 86400,
         "https://mp.weixin.qq.com/s/a2", "含奖学金说明"),
        ("a3", "synthetic-feed-c", "学院讲座预告", NOW_TS - 86400 * 10,
         "https://mp.weixin.qq.com/s/a3", ""),
        ("a4", "synthetic-feed-a", "很旧的文章", NOW_TS - 86400 * 400,
         "https://mp.weixin.qq.com/s/a4", ""),
    ]
    for aid, mpid, title, ts, url, desc in arts:
        conn.execute(
            "INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, mpid, title, "", url, desc, ts,
             "2026-10-27 12:00:00", NOW_TS, 1))
    conn.commit()
    conn.close()


class MpArticlesServiceTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ)
        self._env.start()
        os.environ.pop("ZJGSU_MP_RSS_DB", None)
        self.addCleanup(self._env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "db.db"
        build_db(self.db)
        self.svc = MpArticlesService(db_path=self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ok_default_window(self):
        r = self.svc.get_mp_articles()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["freshness"]["source"], "local_db")
        cov = r["coverage"]
        self.assertEqual(cov["total_matched"], 2)  # a3(10天前)/a4(400天前) 超出 7 天窗
        arts = r["data"]["articles"]
        self.assertEqual(len(arts), 2)
        # 倒序：最新在前
        self.assertEqual(arts[0]["title"], "官微最新推送")
        # 白名单投影 + 时间转换
        self.assertIn("publish_time", arts[0])
        self.assertRegex(arts[0]["publish_time"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_filter_by_mp_name(self):
        r = self.svc.get_mp_articles(mp_name="教务处", days=0)
        self.assertEqual(r["status"], "ok")
        titles = [a["mp_name"] for a in r["data"]["articles"]]
        self.assertTrue(titles and all("教务处" in n for n in titles))

    def test_filter_by_keyword(self):
        r = self.svc.get_mp_articles(keyword="奖学金", days=0)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["coverage"]["total_matched"], 1)
        self.assertEqual(r["data"]["articles"][0]["title"], "关于考试安排的通知")

    def test_days_zero_means_unbounded(self):
        r = self.svc.get_mp_articles(days=0, limit=50)
        self.assertEqual(r["coverage"]["total_matched"], 4)
        self.assertIsNone(r["coverage"]["days_window"])

    def test_recent_window_excludes_future_seconds_and_milliseconds(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE articles SET publish_time=? WHERE id='a1'", (NOW_TS + 86400,))
        conn.execute("UPDATE articles SET publish_time=? WHERE id='a2'", ((NOW_TS + 86400) * 1000,))
        conn.execute("UPDATE articles SET publish_time=? WHERE id='a3'", (NOW_TS - 60,))
        conn.commit()
        conn.close()

        recent = self.svc.get_mp_articles(days=7, limit=50)
        self.assertEqual(recent["status"], "ok")
        self.assertEqual(recent["coverage"]["total_matched"], 1)
        self.assertEqual([item["title"] for item in recent["data"]["articles"]], ["学院讲座预告"])

        all_dates = self.svc.get_mp_articles(days=0, limit=50)
        self.assertEqual(all_dates["status"], "ok")
        self.assertEqual(all_dates["coverage"]["total_matched"], 4)
        titles = {item["title"] for item in all_dates["data"]["articles"]}
        self.assertTrue({"官微最新推送", "关于考试安排的通知"}.issubset(titles))

    def test_limit_clamped(self):
        r = self.svc.get_mp_articles(days=0, limit=999)
        self.assertEqual(r["coverage"]["limit"], 50)  # 上限 50
        r2 = self.svc.get_mp_articles(days=0, limit=2)
        self.assertEqual(len(r2["data"]["articles"]), 2)
        self.assertEqual(r2["coverage"]["returned"], 2)

    def test_missing_db_unavailable(self):
        svc = MpArticlesService(db_path=self.db.parent / "nope.db")
        r = svc.get_mp_articles()
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("不存在或不可读", r["reason"])
        self.assertNotIn("data", r)  # unavailable 不携带数据

    def test_schema_mismatch_unavailable(self):
        bad = self.db.parent / "bad.db"
        conn = sqlite3.connect(bad)
        conn.execute("CREATE TABLE articles (x TEXT)")
        conn.commit()
        conn.close()
        svc = MpArticlesService(db_path=bad)
        r = svc.get_mp_articles()
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("表结构", r["reason"])

    def test_env_override(self):
        import os
        old = os.environ.get("ZJGSU_MP_RSS_DB")
        try:
            os.environ["ZJGSU_MP_RSS_DB"] = str(self.db)
            svc = MpArticlesService()
            self.assertEqual(svc.get_mp_articles()["status"], "ok")
        finally:
            if old is None:
                os.environ.pop("ZJGSU_MP_RSS_DB", None)
            else:
                os.environ["ZJGSU_MP_RSS_DB"] = old

    def test_json_serializable(self):
        r = self.svc.get_mp_articles()
        json.dumps(r, ensure_ascii=False)

    def test_explicit_test_path_takes_precedence_over_process_environment(self):
        with patch.dict(os.environ, {"ZJGSU_MP_RSS_DB": str(self.db.parent / "missing.db")}):
            self.assertEqual(MpArticlesService(db_path=self.db).get_mp_articles()["status"], "ok")

    def test_special_character_path_opens_correct_database_and_is_read_only(self):
        actual = self.db.parent / "local#%中文.db"
        decoy = self.db.parent / "local"
        build_db(actual)
        decoy_conn = sqlite3.connect(decoy)
        decoy_conn.execute("CREATE TABLE decoy (v INTEGER)")
        decoy_conn.commit()
        decoy_conn.close()
        service = MpArticlesService(db_path=actual)
        self.assertEqual(service.get_mp_articles()["coverage"]["total_matched"], 2)
        conn = service._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden_write (v INTEGER)")
        finally:
            conn.close()

    def test_deleted_articles_are_not_returned_or_counted(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE articles SET status=1000 WHERE id='a1'")
        conn.commit()
        conn.close()
        result = self.svc.get_mp_articles(days=0)
        self.assertEqual(result["coverage"]["total_matched"], 3)
        self.assertNotIn("官微最新推送", [a["title"] for a in result["data"]["articles"]])

    def test_millisecond_timestamps_use_same_unit_for_filter_sort_and_display(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE articles SET publish_time=? WHERE id='a1'", ((NOW_TS - 40 * 86400) * 1000,))
        conn.execute("UPDATE articles SET publish_time=? WHERE id='a2'", ((NOW_TS - 600) * 1000,))
        conn.commit()
        conn.close()
        result = self.svc.get_mp_articles(days=7)
        self.assertEqual(result["coverage"]["total_matched"], 1)
        self.assertEqual(result["data"]["articles"][0]["title"], "关于考试安排的通知")
        self.assertRegex(result["data"]["articles"][0]["publish_time"], r"\d{4}-\d{2}-\d{2}")

    def test_mixed_timestamp_units_sort_and_render_correctly(self):
        timestamps = {"a1": NOW_TS - 60, "a2": NOW_TS - 120,
                      "a3": NOW_TS - 180, "a4": NOW_TS - 240}
        conn = sqlite3.connect(self.db)
        for aid, ts in timestamps.items():
            conn.execute("UPDATE articles SET publish_time=? WHERE id=?",
                         (ts * 1000 if aid in {"a2", "a4"} else ts, aid))
        conn.commit()
        conn.close()
        result = self.svc.get_mp_articles(days=7, limit=10)
        self.assertEqual(result["coverage"]["total_matched"], 4)
        articles = result["data"]["articles"]
        self.assertEqual([a["title"] for a in articles], [
            "官微最新推送", "关于考试安排的通知", "学院讲座预告", "很旧的文章"])
        china_tz = timezone(timedelta(hours=8))
        self.assertEqual([a["publish_time"] for a in articles], [
            datetime.fromtimestamp(timestamps[aid], china_tz).strftime("%Y-%m-%d %H:%M")
            for aid in ("a1", "a2", "a3", "a4")])

    def test_offset_can_retrieve_the_remaining_results(self):
        first = self.svc.get_mp_articles(days=0, limit=2, offset=0)
        self.assertTrue(first["pagination"]["has_more"])
        second = self.svc.get_mp_articles(days=0, limit=2, offset=first["pagination"]["next_offset"])
        self.assertFalse(second["pagination"]["has_more"])
        first_titles = {a["title"] for a in first["data"]["articles"]}
        second_titles = {a["title"] for a in second["data"]["articles"]}
        self.assertEqual(len(first_titles | second_titles), 4)
        self.assertFalse(first_titles & second_titles)

    def test_pages_with_equal_timestamps_cover_all_rows_without_duplicates(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE articles SET publish_time=?", (NOW_TS - 600,))
        conn.commit()
        conn.close()
        first = self.svc.get_mp_articles(days=7, limit=2, offset=0)
        second = self.svc.get_mp_articles(days=7, limit=2, offset=first["pagination"]["next_offset"])
        first_titles = {a["title"] for a in first["data"]["articles"]}
        second_titles = {a["title"] for a in second["data"]["articles"]}
        self.assertEqual(len(first_titles | second_titles), 4)
        self.assertFalse(first_titles & second_titles)
        self.assertFalse(second["pagination"]["has_more"])

    def test_count_and_rows_share_one_database_snapshot(self):
        writer = sqlite3.connect(self.db)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.commit()
        reader = self.svc._connect()

        class Interleaved:
            def execute(self, sql, parameters=()):
                cursor = reader.execute(sql, parameters)
                if sql.startswith("SELECT COUNT(*)"):
                    writer.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        "a5", "synthetic-feed-a", "并发新增", "", "https://mp.weixin.qq.com/s/a5", "",
                        NOW_TS - 1, "", "", 1))
                    writer.commit()
                return cursor

            def close(self):
                reader.close()

        try:
            with patch.object(self.svc, "_connect", return_value=Interleaved()):
                result = self.svc.get_mp_articles(days=0, limit=50)
            self.assertEqual(result["coverage"]["total_matched"], result["coverage"]["returned"])
            self.assertEqual(result["coverage"]["returned"], 4)
        finally:
            writer.close()

    def test_metadata_never_claims_last_collection_or_publication_is_known(self):
        result = self.svc.get_mp_articles()
        self.assertIsNone(result["freshness"]["last_successful_collection_at"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("采集时间", result["freshness"]["time_semantics"])
        self.assertNotIn("8/20", json.dumps(result, ensure_ascii=False))

    def test_sanitizes_urls_and_never_returns_private_query_parameters(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE articles SET url=? WHERE id='a1'", ("javascript:alert(1)",))
        conn.execute("UPDATE articles SET url=? WHERE id='a2'", (
            "https://mp.weixin.qq.com/s?__biz=demo&mid=12&idx=1&sn=abc&token=synthetic-secret",))
        conn.commit()
        conn.close()
        result = self.svc.get_mp_articles(days=7)
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("javascript:", text)
        self.assertNotIn("synthetic-secret", text)
        self.assertIn("mp.weixin.qq.com/s", text)

    def test_invalid_arguments_and_database_errors_are_controlled_without_path_leak(self):
        for kwargs in ({"days": -1}, {"limit": -1}, {"offset": -1}, {"days": True}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.svc.get_mp_articles(**kwargs)["status"], "unavailable")
        missing = MpArticlesService(db_path=self.db.parent / "sensitive-local-name.db")
        self.assertNotIn(str(missing.db_path), json.dumps(missing.get_mp_articles(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
