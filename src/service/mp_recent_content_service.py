"""只读汇总近期文章正文；不写库、不按问题关键词预筛候选。"""
from __future__ import annotations

import unicodedata
import time


def _identity_text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


class MpRecentContentService:
    """在调用方提供的列表和单篇正文服务之上做有界的顺序读取。"""

    def __init__(self, articles_service, content_service, clock=None, weread_service=None):
        self.articles_service = articles_service
        self.content_service = content_service
        self.clock = clock or time.monotonic
        self.weread_service = weread_service

    def read_recent(self, mp_name=None, days=7, limit=5, offset=0,
                    scope="school", mp_ids=None, max_total_chars=12000):
        def bad():
            return {"status": "unavailable", "module_id": "mp_recent_content",
                    "reason": "参数无效：请检查来源、时间、页码和字数限制。"}

        for value, lower, upper in ((days, 1, 365), (limit, 1, 10),
                                    (offset, 0, 1_000_000),
                                    (max_total_chars, 500, 30000)):
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                return bad()
        if scope not in ("all", "school", "school_confirmed"):
            return bad()
        if mp_name is not None and (not isinstance(mp_name, str) or not mp_name.strip() or len(mp_name) > 200):
            return bad()
        if mp_ids is not None and (not isinstance(mp_ids, list) or not 1 <= len(mp_ids) <= 50
                                   or any(not isinstance(x, str) or not x.strip() or len(x) > 255 for x in mp_ids)):
            return bad()
        started = self.clock()
        try:
            options = dict(mp_name=mp_name, days=days, limit=limit, offset=offset,
                           scope=scope, mp_ids=mp_ids, count_total=True)
            if self.weread_service is not None:
                options["_include_internal_id"] = True
            listing = self.articles_service.get_mp_articles(**options)
        except Exception:
            return {"status": "unavailable", "module_id": "mp_recent_content",
                    "reason": "无法读取本地文章列表。"}
        if not isinstance(listing, dict) or listing.get("status") not in ("ok", "partial"):
            return {"status": "unavailable", "module_id": "mp_recent_content",
                    "reason": "本地文章列表不可用。"}
        candidates = listing.get("data", {}).get("articles")
        pagination = listing.get("pagination")
        if not isinstance(candidates, list) or len(candidates) > limit or not isinstance(pagination, dict):
            return {"status": "unavailable", "module_id": "mp_recent_content",
                    "reason": "本地文章列表格式无法识别。"}

        records = []
        attempted = succeeded = failed = missing_url = truncated = incomplete = used = 0
        identity_mismatch = identity_unverified = 0
        weread_attempted = weread_succeeded = 0
        weread_stopped = False
        soft_deadline_reached = False
        for candidate in candidates:
            if max_total_chars - used < 500:
                break
            if self.clock() - started >= 25:
                soft_deadline_reached = True
                break
            if not isinstance(candidate, dict):
                records.append({"status": "unavailable", "reason": "文章记录无法识别。"})
                failed += 1
                continue
            record = {key: candidate.get(key) for key in (
                "title", "mp_name", "mp_id", "publish_time", "publish_time_known", "url",
                "url_status", "source_status", "source_category")}
            url = candidate.get("url")
            if not isinstance(url, str) or not url:
                missing_url += 1
            per_article_limit = min(5000, max_total_chars - used)
            result = None
            source = None
            candidate_attempted = False
            if (self.weread_service is not None and not weread_stopped
                    and candidate.get("_article_storage_id") and candidate.get("mp_id")):
                candidate_attempted = True
                weread_attempted += 1
                try:
                    result = self.weread_service.read(
                        candidate["_article_storage_id"], candidate["mp_id"],
                        expected_title=candidate.get("title"), expected_source=candidate.get("mp_name"),
                        max_chars=per_article_limit)
                except Exception:
                    result = None
                if isinstance(result, dict) and result.get("code") in ("auth_required", "auth_or_quota", "rate_limited"):
                    weread_stopped = True
                if isinstance(result, dict) and result.get("status") == "ok":
                    source = "weread"
            if source is None and isinstance(url, str) and url:
                candidate_attempted = True
                try:
                    result = self.content_service.read(url, max_chars=per_article_limit)
                except Exception:
                    result = None
                if isinstance(result, dict) and result.get("status") == "ok":
                    source = "public_wechat_page"
            attempted += int(candidate_attempted)
            data = result.get("data") if isinstance(result, dict) else None
            text = data.get("text") if isinstance(data, dict) else None
            if not isinstance(result, dict) or result.get("status") != "ok" or not isinstance(text, str) or not text:
                record.update(status="unavailable", reason="未能读取可靠的正文文字。")
                failed += 1
                records.append(record)
                continue
            pairs = []
            for field, max_page_length in (("title", 200), ("mp_name", 100)):
                expected = _identity_text(candidate.get(field))
                actual = _identity_text(data.get(field))
                # The page extractor caps metadata length. A capped value cannot
                # establish disagreement with a longer database value.
                comparable = bool(expected and actual and len(actual) < max_page_length
                                  and len(expected) < max_page_length)
                if field == "mp_name" and expected == "未知公众号":
                    comparable = False
                pairs.append((comparable, expected == actual))
            if any(comparable and not equal for comparable, equal in pairs):
                record.update(status="unavailable", identity_status="mismatch",
                              reason="文章页标题或来源与本地记录不一致，正文未采用。")
                failed += 1
                identity_mismatch += 1
                records.append(record)
                continue
            identity_status = "metadata_consistent" if all(p[0] and p[1] for p in pairs) else "unverified"
            identity_unverified += int(identity_status == "unverified")
            body_coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
            was_truncated = body_coverage.get("truncated") is True or len(text) > per_article_limit
            body_complete = body_coverage.get("complete") is True and not was_truncated
            text = text[:per_article_limit]
            used += len(text)
            succeeded += 1
            weread_succeeded += int(source == "weread")
            truncated += int(was_truncated)
            incomplete += int(not body_complete)
            record.update(status="ok", text=text, truncated=was_truncated,
                          content_complete=body_complete, content_source=source,
                          identity_status=identity_status)
            records.append(record)

        unprocessed = len(candidates) - len(records)
        upstream_more = pagination.get("has_more") is True
        has_more = bool(unprocessed or upstream_more)
        partial = bool(listing.get("status") == "partial" or failed or incomplete or identity_unverified or has_more)
        return {
            "status": "partial" if partial else "ok", "module_id": "mp_recent_content",
            "data": {"articles": records},
            "coverage": {"candidate_page": len(candidates), "attempted": attempted,
                         "succeeded": succeeded, "failed": failed, "missing_url": missing_url,
                         "truncated": truncated, "incomplete": incomplete, "unprocessed": unprocessed,
                         "identity_mismatch": identity_mismatch, "identity_unverified": identity_unverified,
                         "weread_attempted": weread_attempted, "weread_succeeded": weread_succeeded,
                         "weread_stopped": weread_stopped,
                         "soft_deadline_reached": soft_deadline_reached,
                         "returned_chars": used, "max_total_chars": max_total_chars,
                         "total_matched": listing.get("coverage", {}).get("total_matched"),
                         "scope_exhausted": not has_more, "complete": False},
            "pagination": {"offset": offset, "has_more": has_more,
                           "next_offset": offset + len(records) if has_more else None,
                           "stable_across_calls": False},
            "scope_info": listing.get("scope_info"), "freshness": listing.get("freshness"),
            "issues": listing.get("issues", []),
            "gaps": ["只读取本地已有订阅，不保证覆盖学校全部发布。",
                     "候选不按问题关键词过滤；微信读书及公开页面均不可读时无法获取正文。",
                     "文字提取可能遗漏图片、表格和视频；未发现答案不代表不存在。",
                     "整批 25 秒软截止：到时不再发下一篇请求；正在进行的单篇请求仍可能令总耗时超过 25 秒。",
                     "返回文字临时存在调用结果中，不保证客户端不会另行保存。",
                     "页面标题及公众号名若齐全则与本地记录比对；缺失时身份未核验，元数据一致也不能保证来源真实性。"],
        }
