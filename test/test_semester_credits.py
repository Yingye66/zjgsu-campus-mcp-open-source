"""Credit aggregation uses synthetic timetable rows without accounts or HTTP."""
import unittest
from copy import deepcopy
from decimal import Decimal, localcontext

from src.service.semester_credits import summarize_timetable_credits


def course(code="SYN101", credit="3", **extra):
    return {"course_code": code, "credit": credit, **extra}


class SemesterCreditSummaryTests(unittest.TestCase):
    def summarize(self, items, **kwargs):
        return summarize_timetable_credits(
            items, complete=kwargs.get("complete", True),
            has_more=kwargs.get("has_more", False), issues=kwargs.get("issues", []))

    def test_week_and_teacher_duplicates_count_each_course_once(self):
        rows = [course(weeks_text="1-8"), course(credit="3.00", weeks_text="9-16"),
                course(teacher_name="Synthetic teacher"), course("SYN102", "1.5")]
        original = deepcopy(rows)
        result = self.summarize(rows)
        self.assertEqual(result["aggregation_scope"], "timetable_visible_courses")
        self.assertEqual(result["visible_course_count"], 2)
        self.assertEqual(result["known_credit_sum"], "4.5")
        self.assertEqual(result["unknown_credit_courses"], [])
        self.assertEqual(result["conflicting_courses"], [])
        self.assertTrue(result["all_visible_credits_known"])
        self.assertTrue(result["source_complete"])
        self.assertEqual(rows, original)

    def test_decimal_addition_is_exact_and_zero_credit_is_known(self):
        result = self.summarize([course("SYN101", 0.1), course("SYN102", "0.2"),
                                 course("SYN103", 0)])
        self.assertEqual(result["known_credit_sum"], "0.3")
        self.assertTrue(result["all_visible_credits_known"])

    def test_decimal_context_does_not_round_large_valid_values(self):
        with localcontext() as ctx:
            ctx.prec = 3
            result = self.summarize([course("SYN101", "123456789.1"),
                                     course("SYN102", "0.2")])
        self.assertEqual(result["known_credit_sum"], "123456789.3")

    def test_extreme_exponents_and_oversized_inputs_are_unknown(self):
        # These compact strings must not expand to a billion output digits or
        # allocate a Decimal context with a billion places of precision.
        oversized = ["1e1000000000", "1e-1000000000", "0e1000000000",
                     "0e-1000000000", "1e128", "1e-129",
                     Decimal("1e1000000000"), Decimal("1e-1000000000")]
        for value in oversized:
            with self.subTest(value=str(value)[:40]):
                result = self.summarize([course(credit=value), course("SYN102", "2")])
                self.assertEqual(result["unknown_credit_courses"], ["SYN101"])
                self.assertEqual(result["known_credit_sum"], "2")
                self.assertFalse(result["all_visible_credits_known"])

    def test_oversized_input_text_is_unknown_before_decimal_parsing(self):
        for value in ["1" * 129, "0." + "1" * 129, " " * 129 + "3"]:
            with self.subTest(length=len(value)):
                result = self.summarize([course(credit=value)])
                self.assertEqual(result["unknown_credit_courses"], ["SYN101"])
                self.assertEqual(result["known_credit_sum"], "0")

    def test_numeric_resource_boundary_keeps_finite_values_within_128_places(self):
        for value, expected in [("1e127", "1" + "0" * 127),
                                ("1e-128", "0." + "0" * 127 + "1")]:
            with self.subTest(value=value):
                result = self.summarize([course(credit=value)])
                self.assertEqual(result["known_credit_sum"], expected)
                self.assertTrue(result["all_visible_credits_known"])

    def test_missing_invalid_and_nonfinite_credits_are_not_zero(self):
        invalid_values = [None, "", " ", "pending", True, False, [], {},
                          "NaN", "Infinity", "-Infinity", float("nan"),
                          float("inf"), "-0.5", Decimal("NaN")]
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                result = self.summarize([course(credit=value), course("SYN102", "2")])
                self.assertEqual(result["known_credit_sum"], "2")
                self.assertEqual(result["unknown_credit_courses"], ["SYN101"])
                self.assertFalse(result["all_visible_credits_known"])

    def test_missing_credit_in_one_duplicate_does_not_borrow_another_value(self):
        result = self.summarize([course(), course(credit=None)])
        self.assertEqual(result["visible_course_count"], 1)
        self.assertEqual(result["known_credit_sum"], "0")
        self.assertEqual(result["unknown_credit_courses"], ["SYN101"])

    def test_conflicting_duplicate_credits_never_choose_the_first(self):
        for rows in ([course(credit="3"), course(credit="5")],
                     [course(credit="5"), course(credit="3")]):
            result = self.summarize(rows + [course("SYN102", "1")])
            self.assertEqual(result["known_credit_sum"], "1")
            self.assertEqual(result["conflicting_courses"], ["SYN101"])
            self.assertFalse(result["all_visible_credits_known"])

    def test_unidentified_rows_are_counted_and_never_silently_summed(self):
        result = self.summarize([course(), course(None, "10"), course("", "20"),
                                 course(" ", "30"), course(True, "40"),
                                 {"credit": "50"}])
        self.assertEqual(result["visible_course_count"], 1)
        self.assertEqual(result["known_credit_sum"], "3")
        self.assertEqual(result["unidentified_row_count"], 5)
        self.assertFalse(result["all_visible_credits_known"])
        self.assertTrue(result["notes"])

    def test_local_pagination_prevents_source_total_claim(self):
        result = self.summarize([course()], has_more=True)
        self.assertFalse(result["source_complete"])
        self.assertTrue(result["all_visible_credits_known"])
        self.assertTrue(result["notes"])

    def test_omitted_practical_blocks_prevent_source_total_claim(self):
        result = self.summarize([course()], issues=[{
            "code": "adjacent_data_omitted", "safe_message": "PRIVATE_MARKER_DO_NOT_COPY"}])
        self.assertFalse(result["source_complete"])
        self.assertTrue(result["notes"])
        self.assertNotIn("PRIVATE_MARKER_DO_NOT_COPY", repr(result))

    def test_unproven_source_or_issue_prevents_source_total_claim(self):
        for kwargs in ({"complete": False}, {"issues": [{"code": "completeness_unproven"}]}):
            with self.subTest(kwargs=kwargs):
                result = self.summarize([course()], **kwargs)
                self.assertEqual(result["known_credit_sum"], "3")
                self.assertFalse(result["source_complete"])
                self.assertTrue(result["notes"])

    def test_empty_visible_snapshot_remains_a_scoped_result(self):
        result = self.summarize([], complete=False)
        self.assertEqual(result["visible_course_count"], 0)
        self.assertEqual(result["known_credit_sum"], "0")
        self.assertFalse(result["source_complete"])
        self.assertTrue(result["notes"])


if __name__ == "__main__":
    unittest.main()
