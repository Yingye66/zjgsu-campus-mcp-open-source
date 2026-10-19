"""Per-request timings without retaining request or response content."""
from __future__ import annotations

import time


class TimingTransport:
    def __init__(self, delegate, monotonic_fn=None):
        self._delegate = delegate
        self._time = monotonic_fn or time.perf_counter
        self.requests: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def _call(self, method, operation, *args, **kwargs):
        started = self._time()
        outcome, status = "exception", None
        try:
            result = operation(*args, **kwargs)
            outcome = ("network_error" if isinstance(result, dict)
                       and result.get("kind") == "network_error" else "response")
            if isinstance(result, dict):
                candidate = result.get("status")
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    status = candidate
            return result
        finally:
            self.requests.append({
                "method": method,
                "elapsed_ms": round(max(0.0, self._time() - started) * 1000, 3),
                "http_status": status,
                "outcome": outcome,
            })

    def get(self, url, headers=None, timeout=20):
        return self._call("GET", self._delegate.get, url,
                          headers=headers, timeout=timeout)

    def post_form(self, url, form=None, headers=None, timeout=20):
        return self._call("POST", self._delegate.post_form, url, form,
                          headers=headers, timeout=timeout)
