"""Request timings keep payloads and credentials out of diagnostics."""
from copy import deepcopy
import unittest

from src.service.request_timing import TimingTransport


class ScriptedTransport:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []
        self.cookies = None

    def _call(self, method, url, form, headers, timeout):
        self.calls.append((method, url, form, headers, timeout))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result

    def get(self, url, headers=None, timeout=20):
        return self._call("GET", url, None, headers, timeout)

    def post_form(self, url, form=None, headers=None, timeout=20):
        return self._call("POST", url, form, headers, timeout)

    def set_cookies(self, cookies):
        self.cookies = cookies


class RequestTimingTests(unittest.TestCase):
    def test_two_request_durations_follow_order_and_preserve_payloads(self):
        first = {"kind": "response", "status": 200, "text": "private-body"}
        second = {"kind": "response", "status": 503, "text": "upstream-body"}
        original = deepcopy([first, second])
        delegate = ScriptedTransport([first, second])
        ticks = iter([10.0, 10.1, 11.0, 11.25])
        timed = TimingTransport(delegate, monotonic_fn=lambda: next(ticks))
        headers = {"Authorization": "PRIVATE-HEADER"}
        form = {"password": "PRIVATE-FORM"}

        self.assertIs(timed.get("https://example.invalid/private", headers, 7), first)
        self.assertIs(timed.post_form("https://example.invalid/query", form,
                                     headers=headers, timeout=9), second)
        self.assertEqual(delegate.calls, [
            ("GET", "https://example.invalid/private", None, headers, 7),
            ("POST", "https://example.invalid/query", form, headers, 9),
        ])
        self.assertEqual([first, second], original)
        self.assertEqual(timed.requests, [
            {"method": "GET", "elapsed_ms": 100.0,
             "http_status": 200, "outcome": "response"},
            {"method": "POST", "elapsed_ms": 250.0,
             "http_status": 503, "outcome": "response"},
        ])
        self.assertIsInstance(timed.requests[0]["elapsed_ms"], float)

    def test_network_error_records_no_private_response_fields(self):
        response = {"kind": "network_error", "err": "PRIVATE-ERROR-URL",
                    "status": None, "text": "PRIVATE-BODY"}
        delegate = ScriptedTransport([response])
        ticks = iter([1.0, 1.5])
        timed = TimingTransport(delegate, monotonic_fn=lambda: next(ticks))

        self.assertIs(timed.get("https://example.invalid/?ticket=PRIVATE-TICKET"),
                      response)
        self.assertEqual(timed.requests, [
            {"method": "GET", "elapsed_ms": 500.0,
             "http_status": None, "outcome": "network_error"},
        ])
        self.assertNotIn("PRIVATE", repr(timed.requests))

    def test_exception_is_recorded_without_text_and_same_exception_propagates(self):
        error = RuntimeError("PRIVATE-EXCEPTION-TICKET")
        ticks = iter([2.0, 2.125])
        timed = TimingTransport(ScriptedTransport([error]),
                                monotonic_fn=lambda: next(ticks))

        with self.assertRaises(RuntimeError) as raised:
            timed.post_form("https://example.invalid/PRIVATE-PATH",
                            {"password": "PRIVATE-PASSWORD"})
        self.assertIs(raised.exception, error)
        self.assertEqual(timed.requests, [
            {"method": "POST", "elapsed_ms": 125.0,
             "http_status": None, "outcome": "exception"},
        ])
        self.assertNotIn("PRIVATE", repr(timed.requests))

    def test_non_integer_status_cannot_enter_public_diagnostics(self):
        for status in ("PRIVATE-STATUS", True, None):
            with self.subTest(status=status):
                ticks = iter([0.0, 0.001])
                response = {"kind": "response", "status": status}
                timed = TimingTransport(ScriptedTransport([response]),
                                        monotonic_fn=lambda: next(ticks))
                self.assertIs(timed.get("https://example.invalid/"), response)
                self.assertIsNone(timed.requests[0]["http_status"])
                self.assertEqual(timed.requests[0]["outcome"], "response")

    def test_other_transport_operations_are_forwarded_without_fake_requests(self):
        delegate = ScriptedTransport([])
        timed = TimingTransport(delegate)
        cookies = [{"name": "synthetic", "value": "SYNTHETIC"}]

        timed.set_cookies(cookies)
        self.assertIs(delegate.cookies, cookies)
        self.assertEqual(timed.requests, [])


if __name__ == "__main__":
    unittest.main()
