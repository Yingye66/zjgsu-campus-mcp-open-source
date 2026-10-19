"""Bounded, session-bound MCP result reuse. No new disk cache or background work."""
from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from threading import Lock, RLock


class QueryCache:
    def __init__(self, ttl_seconds=60, max_entries=32, monotonic_fn=None):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._time = monotonic_fn or time.monotonic
        self._entries = OrderedDict()
        self._guard = RLock()
        self._flights = {}

    @contextmanager
    def _flight(self, key):
        # Serialize calls to one tool/session, without blocking unrelated tools.
        with self._guard:
            lock, users = self._flights.get(key, (Lock(), 0))
            self._flights[key] = lock, users + 1
        try:
            with lock:
                yield
        finally:
            with self._guard:
                _, users = self._flights[key]
                if users == 1:
                    del self._flights[key]
                else:
                    self._flights[key] = lock, users - 1

    def _mark(self, result, *, hit, remaining=0):
        result = deepcopy(result)
        result["freshness"].update(cache_hit=hit, cache_ttl_seconds=self.ttl_seconds,
                                   cache_remaining_seconds=remaining)
        return result

    @staticmethod
    def _current(service, account, result):
        """An overwritten snapshot must never return an obsolete next_cursor."""
        blocks = result["coverage"]["source_snapshots"]
        if len(blocks) != 1:
            return False
        block = blocks[0]
        snapshot = service.store.get_snapshot(account, result["module_id"],
                                               block["source_id"], block["query_scope"])
        if not snapshot or snapshot["snapshot_id"] != result["pagination"]["result_snapshot_id"]:
            return False
        health = service._health_row(account, result["module_id"], block["source_id"])
        return not (health and health.get("last_query_scope") == block["query_scope"]
                    and health.get("last_issue"))

    @staticmethod
    def _update_age(service, result):
        now = service._now_fn()
        freshness = result["freshness"]
        for row in [freshness, *freshness.get("by_source", [])]:
            stamp = row.get("last_success_at")
            if stamp:
                row["data_age_seconds"] = max(0, int((now - datetime.fromisoformat(stamp)).total_seconds()))

    def call(self, method, *args, force_refresh=False, **kwargs):
        arguments = inspect.signature(method).bind(*args, **kwargs)
        arguments.apply_defaults()
        # Cursors read a fixed existing snapshot; refresh only applies to page one.
        if arguments.arguments.get("cursor"):
            return self._mark(method(*args, **kwargs), hit=False)
        service = method.__self__
        account, session = service._ctx()
        if not account or not session:
            return self._mark(method(*args, **kwargs), hit=False)
        fingerprint = hashlib.sha256(json.dumps(session, sort_keys=True,
                                                ensure_ascii=False).encode("utf-8")).digest()
        context = (id(service), account, fingerprint, method.__name__)
        scope_arguments = {k: v for k, v in arguments.arguments.items() if k not in ("cursor", "limit")}
        group = (context, json.dumps(scope_arguments, sort_keys=True, ensure_ascii=False))
        key = (group, json.dumps(arguments.arguments, sort_keys=True, ensure_ascii=False))
        with self._flight(context):
            # A queued caller may have changed accounts while waiting for the lock.
            if service._ctx() != (account, session):
                return self._mark(method(*args, **kwargs), hit=False)
            with self._guard:
                entry = self._entries.get(key)
            if not force_refresh and entry is not None:
                created, saved = entry
                elapsed = self._time() - created
                if (0 <= elapsed < self.ttl_seconds and self._current(service, account, saved)
                        and service._ctx() == (account, session)):
                    result = self._mark(saved, hit=True,
                                        remaining=max(0, int(self.ttl_seconds - elapsed)))
                    self._update_age(service, result)
                    with self._guard:
                        if key in self._entries:
                            self._entries.move_to_end(key)
                    return result
            # Evict sibling page-size variants before refresh, including failed refreshes.
            with self._guard:
                for old_key in list(self._entries):
                    if old_key[0] == group:
                        del self._entries[old_key]
            result = method(*args, **kwargs)
            # No cached errors, partial observations or negative results.
            after_account, after_session = service._ctx()
            if (result.get("status") == "ok" and result.get("items")
                    and (after_account, after_session) == (account, session)
                    and self._current(service, account, result)):
                with self._guard:
                    self._entries[key] = (self._time(), deepcopy(result))
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
            return self._mark(result, hit=False)
