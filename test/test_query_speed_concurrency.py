"""Concurrency regressions with synthetic sessions, fake HTTP and real SQLite."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest import TestCase, main
from unittest.mock import patch

from src.service.campus_service import CampusService
from src.service.query_cache import QueryCache
from src.storage.store import SnapshotStore
from test.helpers import Clock, FakeTransport, course_item, course_ok, seed_session, temp_dir


RANGE_A = ("2026-10-12", "2026-10-18")
RANGE_B = ("2026-10-19", "2026-10-25")
ACCOUNT_A = "acct-concurrency-synthetic-a"
ACCOUNT_B = "acct-concurrency-synthetic-b"


class QuerySpeedConcurrencyTest(TestCase):
    def setUp(self):
        temporary = temp_dir()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.clock = Clock()
        self.http = FakeTransport([
            course_ok([course_item(id="synthetic-A")]),
            course_ok([course_item(id="synthetic-B")]),
        ])
        self.store = SnapshotStore(self.data / "cache.sqlite3")
        self.service = CampusService(self.store, self.data, transport=self.http,
                                     now_fn=self.clock.now)
        self.cache = QueryCache(monotonic_fn=lambda: self.clock.t.timestamp())
        seed_session(self.data, ACCOUNT_A)

    def test_account_switch_while_queued_cannot_return_previous_account_cache(self):
        first = self.cache.call(self.service.get_timetable, *RANGE_A)
        self.assertEqual(first["items"][0]["entry_id"], "synthetic-A")
        # Hold the actual per-context lock so the next call captures A then waits.
        context = next(iter(self.cache._entries))[0]
        context_captured = Event()
        original_context = self.service._ctx

        def observed_context():
            result = original_context()
            context_captured.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(self.service, "_ctx", side_effect=observed_context):
                with self.cache._flight(context):
                    pending = pool.submit(self.cache.call,
                                          self.service.get_timetable, *RANGE_A)
                    self.assertTrue(context_captured.wait(5), "query did not capture its context")
                    seed_session(self.data, ACCOUNT_B)
                result = pending.result(timeout=5)

        self.assertFalse(result["freshness"]["cache_hit"])
        self.assertEqual(result["items"][0]["entry_id"], "synthetic-B")
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.http.calls[-1]["headers"]["zjgsuToken"],
                         "TEST_TOKEN_" + ACCOUNT_B)

    def test_concurrent_lru_eviction_does_not_crash_or_replace_newer_entry(self):
        self.cache = QueryCache(max_entries=1,
                                monotonic_fn=lambda: self.clock.t.timestamp())
        first = self.cache.call(self.service.get_timetable, *RANGE_A)
        other = CampusService(self.store, self.data, transport=self.http,
                              now_fn=self.clock.now)
        checking_snapshot = Event()
        resume_check = Event()
        original_snapshot = self.store.get_snapshot
        first_scope = "%s~%s" % RANGE_A

        def paused_snapshot(account, module, source, scope):
            result = original_snapshot(account, module, source, scope)
            if scope == first_scope:
                checking_snapshot.set()
                if not resume_check.wait(5):
                    raise AssertionError("snapshot check was not released")
            return result

        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(self.store, "get_snapshot", side_effect=paused_snapshot):
                pending = pool.submit(self.cache.call,
                                      self.service.get_timetable, *RANGE_A)
                try:
                    self.assertTrue(checking_snapshot.wait(5), "cache validation did not start")
                    inserted = self.cache.call(other.get_timetable, *RANGE_B)
                finally:
                    resume_check.set()
                resumed = pending.result(timeout=5)

        self.assertTrue(resumed["freshness"]["cache_hit"])
        self.assertEqual(resumed["items"], first["items"])
        self.assertEqual(inserted["items"][0]["entry_id"], "synthetic-B")
        still_cached = self.cache.call(other.get_timetable, *RANGE_B)
        self.assertTrue(still_cached["freshness"]["cache_hit"])
        self.assertEqual(still_cached["items"], inserted["items"])
        self.assertEqual(len(self.http.calls), 2)


if __name__ == "__main__":
    main()
