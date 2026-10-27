"""The shared educational transport sends its configured browser header."""
import unittest

from src.adapters.campus.jwweb import JwwebTransport, UA


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"ok"


class _Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return _Response()


class UserAgentTests(unittest.TestCase):
    def test_all_transport_methods_emit_browser_header_without_network(self):
        transport = JwwebTransport()
        opener = _Opener()
        transport._opener = opener
        transport.get("https://example.invalid/synthetic")
        transport.get_binary("https://example.invalid/synthetic")
        transport.post_form("https://example.invalid/synthetic", {"example": "value"})
        self.assertEqual(len(opener.requests), 3)
        self.assertIn("Chrome/", UA)
        for request in opener.requests:
            self.assertEqual(request.get_header("User-agent"), UA)


if __name__ == "__main__":
    unittest.main()
