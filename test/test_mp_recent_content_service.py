"""Recent article body aggregation, using synthetic data only."""
import unittest

from src.service.mp_recent_content_service import MpRecentContentService


def article(title, url, source="示例食堂"):
    return {"title": title, "mp_name": source, "mp_id": "feed-1",
            "publish_time": "2026-10-28", "publish_time_known": True,
            "url": url, "url_status": "format_accepted_unverified",
            "source_status": "confirmed", "source_category": "食堂"}


class FakeList:
    def __init__(self, articles, has_more=False, status="ok"):
        self.articles, self.has_more, self.status, self.calls = articles, has_more, status, []

    def get_mp_articles(self, **kwargs):
        self.calls.append(kwargs)
        offset, limit = kwargs["offset"], kwargs["limit"]
        return {"status": self.status, "data": {"articles": self.articles[offset:offset + limit]},
                "coverage": {"total_matched": len(self.articles)},
                "pagination": {"has_more": self.has_more or offset + limit < len(self.articles)},
                "scope_info": {}, "issues": [], "gaps": [], "freshness": {}}


class FakeBody:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def read(self, url, max_chars):
        self.calls.append((url, max_chars))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


def body(text, *, truncated=False, complete=True, title=None, mp_name=None):
    return {"status": "ok", "data": {"text": text, "title": title, "mp_name": mp_name},
            "coverage": {"truncated": truncated, "complete": complete}}


class RecentContentTests(unittest.TestCase):
    def test_weread_fallback_reads_body_without_public_url(self):
        row = article("校园生活", None)
        row["_article_storage_id"] = "synthetic-id"
        listing = FakeList([row])
        class Reader:
            def read(self, article_id, mp_id, **kwargs):
                self.arguments = (article_id, mp_id, kwargs)
                return body("新品香菇面 12 元", title="校园生活", mp_name="示例食堂")
        reader = Reader()
        result = MpRecentContentService(listing, FakeBody({}), weread_service=reader).read_recent(limit=1)
        self.assertEqual(result["data"]["articles"][0]["text"], "新品香菇面 12 元")
        self.assertEqual(result["data"]["articles"][0]["content_source"], "weread")
        self.assertNotIn("synthetic-id", str(result))
        self.assertEqual(reader.arguments[0], "synthetic-id")

    def test_auth_failure_stops_repeated_weread_requests(self):
        rows = [article("一", None), article("二", None)]
        for row in rows:
            row["_article_storage_id"] = "synthetic-id"
        class Reader:
            calls = 0
            def read(self, *args, **kwargs):
                self.calls += 1
                return {"status": "unavailable", "code": "auth_or_quota"}
        reader = Reader()
        result = MpRecentContentService(FakeList(rows), FakeBody({}), weread_service=reader).read_recent(limit=2)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(result["coverage"]["failed"], 2)
        self.assertEqual(result["status"], "partial")

    def test_body_only_question_does_not_filter_candidates_by_keyword(self):
        listing = FakeList([article("本周校园生活", "one"), article("食堂上新", "two")])
        reader = FakeBody({"one": body("新品菌菇面 12 元"), "two": body("鸡腿饭 15 元")})
        result = MpRecentContentService(listing, reader).read_recent(mp_name="食堂", days=7, limit=2)
        self.assertEqual(listing.calls[0]["mp_name"], "食堂")
        self.assertNotIn("keyword", listing.calls[0])
        self.assertNotIn("keywords", listing.calls[0])
        self.assertEqual([x["text"] for x in result["data"]["articles"]], ["新品菌菇面 12 元", "鸡腿饭 15 元"])
        self.assertEqual(result["coverage"]["succeeded"], 2)
        self.assertFalse(result["coverage"]["complete"])

    def test_missing_url_and_failure_are_counted_not_silently_empty(self):
        listing = FakeList([article("无链接", None), article("拦截", "two")])
        reader = FakeBody({"two": {"status": "unavailable", "reason": "拦截"}})
        result = MpRecentContentService(listing, reader).read_recent(limit=2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["failed"], 2)
        self.assertEqual(result["coverage"]["missing_url"], 1)
        self.assertEqual(result["coverage"]["attempted"], 1)
        self.assertEqual(len(result["data"]["articles"]), 2)

    def test_truncation_and_media_mark_partial(self):
        listing = FakeList([article("截断", "one"), article("图片", "two")])
        reader = FakeBody({"one": body("a", truncated=True, complete=False),
                           "two": body("b", complete=False)})
        result = MpRecentContentService(listing, reader).read_recent(limit=2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["truncated"], 1)
        self.assertEqual(result["coverage"]["incomplete"], 2)

    def test_total_budget_stops_before_next_fetch_and_can_resume(self):
        listing = FakeList([article("一", "one"), article("二", "two")])
        reader = FakeBody({"one": body("a" * 500), "two": body("b" * 500)})
        result = MpRecentContentService(listing, reader).read_recent(limit=2, max_total_chars=500)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(result["coverage"]["unprocessed"], 1)
        self.assertEqual(result["pagination"]["next_offset"], 1)
        self.assertEqual(result["status"], "partial")

    def test_soft_deadline_stops_before_next_request_and_can_resume(self):
        listing = FakeList([article("一", "one"), article("二", "two")])
        reader = FakeBody({"one": body("a"), "two": body("b")})
        ticks = iter([0.0, 0.0, 25.0])
        result = MpRecentContentService(listing, reader, clock=lambda: next(ticks)).read_recent(limit=2)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(result["coverage"]["unprocessed"], 1)
        self.assertTrue(result["coverage"]["soft_deadline_reached"])
        self.assertEqual(result["pagination"]["next_offset"], 1)
        self.assertEqual(result["status"], "partial")

    def test_has_more_and_upstream_partial_remain_partial(self):
        listing = FakeList([article("一", "one")], has_more=True, status="partial")
        result = MpRecentContentService(listing, FakeBody({"one": body("内容")})).read_recent(limit=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pagination"]["next_offset"], 1)

    def test_exception_does_not_leak_message_and_continues(self):
        listing = FakeList([article("一", "one"), article("二", "two")])
        reader = FakeBody({"one": RuntimeError("secret-detail"), "two": body("正文")})
        result = MpRecentContentService(listing, reader).read_recent(limit=2)
        self.assertNotIn("secret-detail", str(result))
        self.assertEqual(result["coverage"]["failed"], 1)
        self.assertEqual(result["coverage"]["succeeded"], 1)

    def test_wrong_page_title_or_source_is_not_attributed_to_candidate(self):
        listing = FakeList([article("一", "one"), article("二", "two"), article("三", "three")])
        reader = FakeBody({"one": body("正文一", title="另一篇", mp_name="示例食堂"),
                           "two": body("正文二", title="二", mp_name="其他来源"),
                           "three": body("正文三", title="三", mp_name="示例食堂")})
        result = MpRecentContentService(listing, reader).read_recent(limit=3)
        records = result["data"]["articles"]
        self.assertEqual([x["status"] for x in records], ["unavailable", "unavailable", "ok"])
        self.assertNotIn("正文一", str(records))
        self.assertNotIn("正文二", str(records))
        self.assertEqual(records[2]["identity_status"], "metadata_consistent")
        self.assertEqual(result["coverage"]["identity_mismatch"], 2)
        self.assertEqual(result["coverage"]["failed"], 2)

    def test_missing_page_metadata_is_explicitly_unverified_and_partial(self):
        listing = FakeList([article("一", "one")])
        reader = FakeBody({"one": body("正文", title="一")})
        result = MpRecentContentService(listing, reader).read_recent(limit=1)
        self.assertEqual(result["data"]["articles"][0]["identity_status"], "unverified")
        self.assertEqual(result["coverage"]["identity_unverified"], 1)
        self.assertEqual(result["status"], "partial")

    def test_list_failure_stops_and_body_never_called(self):
        listing, reader = FakeList([], status="unavailable"), FakeBody({})
        result = MpRecentContentService(listing, reader).read_recent()
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(reader.calls)

    def test_bad_parameters_do_not_call_any_services(self):
        listing, reader = FakeList([]), FakeBody({})
        service = MpRecentContentService(listing, reader)
        for values in ({"limit": True}, {"limit": 11}, {"days": 0}, {"days": False},
                       {"offset": -1}, {"max_total_chars": 499}, {"scope": "invalid"},
                       {"mp_ids": []}, {"mp_name": ""}):
            with self.subTest(values=values):
                self.assertEqual(service.read_recent(**values)["status"], "unavailable")
        self.assertFalse(listing.calls)
        self.assertFalse(reader.calls)


if __name__ == "__main__":
    unittest.main()
