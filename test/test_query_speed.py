"""Query speed regression checks; synthetic accounts and HTTP only."""
import importlib
import os
import unittest
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test.helpers import Clock, FakeTransport, course_item, course_ok, seed_session, temp_dir
from src.service.campus_service import CampusService
from src.storage.store import SnapshotStore
from src.auth import session as S
from src.service.query_cache import QueryCache
from test.helpers import network_err


class QuerySpeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.clock = Clock()
        self.http = FakeTransport([course_ok([course_item()]) for _ in range(8)])
        self.store = SnapshotStore(self.data / "cache.sqlite3")
        self.service = CampusService(self.store, self.data, transport=self.http,
                                     now_fn=self.clock.now)
        seed_session(self.data, "acct-synthetic-speed")
        with patch.dict(os.environ, {"ZJGSU_CAMPUS_DATA_DIR": str(self.data)}):
            self.server = importlib.import_module("src.mcp_server.server")
        p = patch.object(self.server, "service", self.service)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(self.server, "query_cache", QueryCache(
            monotonic_fn=lambda: self.clock.t.timestamp()))
        p.start()
        self.addCleanup(p.stop)

    def call(self, **kwargs):
        return self.server.get_timetable("2026-10-12", "2026-10-18", **kwargs).structured_content

    def test_repeated_timetable_avoids_second_school_request(self):
        first = self.server.get_timetable("2026-10-12", "2026-10-18")
        second = self.server.get_timetable("2026-10-12", "2026-10-18")
        self.assertEqual(first.structured_content["items"], second.structured_content["items"])
        self.assertEqual(len(self.http.calls), 1)

    def test_hit_preserves_fetch_time_and_updates_age_and_text(self):
        first = self.call()
        self.clock.advance(10)
        result = self.server.get_timetable("2026-10-12", "2026-10-18")
        cached = result.structured_content
        self.assertTrue(cached["freshness"]["cache_hit"])
        self.assertFalse(first["freshness"]["cache_hit"])
        self.assertEqual(cached["freshness"]["data_age_seconds"], 10)
        self.assertEqual(cached["freshness"]["cache_remaining_seconds"], 50)
        self.assertEqual(cached["source_refs"], first["source_refs"])
        self.assertEqual(json.loads(result.content[0].text), cached)
        self.assertFalse(cached["coverage"]["source_snapshots"][0]["complete"])

    def test_expiry_and_force_refresh_request_again(self):
        first = self.call()
        self.clock.advance(59)
        self.assertTrue(self.call()["freshness"]["cache_hit"])
        self.clock.advance(1)
        second = self.call()
        self.assertFalse(second["freshness"]["cache_hit"])
        third = self.call(force_refresh=True)
        self.assertFalse(third["freshness"]["cache_hit"])
        self.assertEqual(len(self.http.calls), 3)
        self.assertNotEqual(first["pagination"]["result_snapshot_id"], second["pagination"]["result_snapshot_id"])

    def test_logout_and_session_rotation_never_reuse(self):
        self.call()
        session = S.load_session(self.data, "acct-synthetic-speed")
        session["zjgsu_token"] = "SYNTHETIC_ROTATED"
        S.save_session(self.data, "acct-synthetic-speed", session)
        self.assertFalse(self.call()["freshness"]["cache_hit"])
        self.assertEqual(len(self.http.calls), 2)
        S.clear_active(self.data)
        result = self.call()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])
        self.assertEqual(len(self.http.calls), 2)

    def test_other_account_scope_and_invalid_parameters_are_isolated(self):
        self.call()
        seed_session(self.data, "acct-second-synthetic")
        self.assertFalse(self.call()["freshness"]["cache_hit"])
        self.server.get_timetable("2026-10-19", "2026-10-25")
        self.assertEqual(len(self.http.calls), 3)
        result = self.call(limit=True)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["issues"][0]["code"], "invalid_arguments")
        self.assertEqual(len(self.http.calls), 3)

    def test_failed_force_refresh_does_not_restore_ok_cache(self):
        first = self.call()
        self.http.queue = [network_err(), network_err()]
        refreshed = self.call(force_refresh=True)
        following = self.call()
        self.assertEqual(len(self.http.calls), 3)
        for result in (refreshed, following):
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["freshness"]["cache_hit"])
            self.assertEqual(result["source_refs"], first["source_refs"])

    def test_empty_results_are_always_rechecked(self):
        self.http.queue = [course_ok([]), course_ok([])]
        self.assertEqual(self.call()["items"], [])
        self.assertEqual(self.call()["items"], [])
        self.assertEqual(len(self.http.calls), 2)

    def test_page_size_change_and_external_snapshot_refresh_invalidate(self):
        self.http.queue = [course_ok([course_item(id="1"), course_item(id="2")]) for _ in range(4)]
        first = self.call(limit=1)
        self.assertTrue(first["pagination"]["has_more"])
        self.call(limit=2)
        third = self.call(limit=1)
        self.assertFalse(third["freshness"]["cache_hit"])
        self.service.get_timetable("2026-10-12", "2026-10-18", limit=1)
        self.http.queue.append(course_ok([course_item()]))
        fourth = self.call(limit=1)
        self.assertFalse(fourth["freshness"]["cache_hit"])
        self.assertEqual(len(self.http.calls), 5)

    def test_cursor_with_force_refresh_keeps_fixed_snapshot_without_network(self):
        self.http.queue = [course_ok([course_item(id="1"), course_item(id="2")])]
        first = self.call(limit=1)
        second = self.call(cursor=first["pagination"]["next_cursor"], limit=1, force_refresh=True)
        self.assertEqual(second["items"][0]["entry_id"], "2")
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(second["pagination"]["result_snapshot_id"], first["pagination"]["result_snapshot_id"])

    def test_mutating_return_value_does_not_modify_cached_result(self):
        first = self.call()
        first["items"][0]["course_name"] = "changed-by-client"
        second = self.call()
        self.assertEqual(second["items"][0]["course_name"], "课程A")
        second["items"][0]["course_name"] = "changed-again"
        self.assertEqual(self.call()["items"][0]["course_name"], "课程A")

    def test_simultaneous_identical_queries_share_one_fetch(self):
        original = self.http.post_form
        def slow(*args, **kwargs):
            time.sleep(0.02)
            return original(*args, **kwargs)
        with patch.object(self.http, "post_form", side_effect=slow):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.call(), range(4)))
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(sum(r["freshness"]["cache_hit"] for r in results), 3)

    def test_entry_limit_evicts_oldest_success(self):
        self.server.query_cache = QueryCache(max_entries=2)
        # Different service instances are separate contexts, even with the same session.
        services = [CampusService(self.store, self.data, transport=self.http,
                                  now_fn=self.clock.now) for _ in range(3)]
        for service in services:
            self.server.query_cache.call(service.get_timetable, "2026-10-12", "2026-10-18")
        self.server.query_cache.call(services[0].get_timetable, "2026-10-12", "2026-10-18")
        self.assertEqual(len(self.http.calls), 4)


if __name__ == "__main__":
    unittest.main()
