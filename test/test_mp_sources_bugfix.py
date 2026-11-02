"""合成来源登记回归：别名便利输入不能放松 ID 身份与排除规则。"""
import unittest
from pathlib import Path

from src.service.mp_articles_service import MpArticlesService
from test.helpers import temp_dir
from test.test_mp_articles_scope import build_db, write_registry


class RegistryAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "synthetic.db"
        self.registry = root / "synthetic-sources.json"
        build_db(self.db)
        self.service = MpArticlesService(db_path=self.db, sources_path=self.registry)

    def test_single_string_alias_preserves_id_based_exclusion(self):
        write_registry(self.registry, [{"id": "feed-hz", "name": "示例名称",
                                        "status": "excluded", "aliases": "部门简称"}])
        result = self.service.get_mp_articles(scope="school", days=0, limit=50)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope_info"]["scope_sources"]["excluded"], 1)
        self.assertNotIn("feed-hz", {item["mp_id"] for item in result["data"]["articles"]})

    def test_malformed_nonstring_alias_still_refuses_school_scope(self):
        for malformed in (42, 0, False, {}, [42]):
            with self.subTest(aliases=malformed):
                write_registry(self.registry, [{"id": "feed-hz", "name": "示例名称",
                                                "status": "excluded", "aliases": malformed}])
                result = self.service.get_mp_articles(scope="school", days=0)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("data", result)

    def test_same_id_different_names_still_shows_actual_feed_name(self):
        write_registry(self.registry, [
            {"id": "feed-jwc", "name": "旧名称", "status": "confirmed"},
            {"id": "feed-jwc", "name": "新名称", "status": "confirmed"},
        ])
        result = self.service.get_mp_articles(scope="school_confirmed", days=0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual({item["mp_id"] for item in result["data"]["articles"]}, {"feed-jwc"})
        self.assertEqual({item["mp_name"] for item in result["data"]["articles"]},
                         {"浙江工商大学教务处"})

    def test_duplicate_id_with_opposed_status_still_refuses(self):
        write_registry(self.registry, [
            {"id": "feed-jwc", "name": "旧名称", "status": "confirmed", "aliases": "简称"},
            {"id": "feed-jwc", "name": "新名称", "status": "excluded"},
        ])
        result = self.service.get_mp_articles(scope="school", days=0)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)
        self.assertIn("registry_conflict", {issue["code"] for issue in result["issues"]})
