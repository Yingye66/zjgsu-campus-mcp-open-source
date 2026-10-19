"""Summarize visible timetable credits without claiming an enrollment total."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext


_MAX_NUMBER_TEXT = 128
_MAX_INTEGER_PLACES = 128
_MAX_FRACTIONAL_PLACES = 128


def _credit(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        text = str(value)
        if len(text) > _MAX_NUMBER_TEXT:
            return None
        result = Decimal(text.strip())
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if not result.is_finite() or result < 0:
        return None
    # Bounds protect parsing, precision allocation and fixed-point formatting;
    # they are resource limits, not a rule about permitted school credits.
    integer_places = max(1, result.adjusted() + 1)
    fractional_places = max(0, -result.as_tuple().exponent)
    if (integer_places > _MAX_INTEGER_PLACES
            or fractional_places > _MAX_FRACTIONAL_PLACES):
        return None
    return result


def _exact_sum(values: list[Decimal]) -> str:
    if not values:
        return "0"
    # Do not inherit a caller's low Decimal precision or round a large total.
    integer_digits = max(1, max(value.adjusted() + 1 for value in values))
    fractional_digits = max(0, -min(value.as_tuple().exponent for value in values))
    with localcontext() as ctx:
        ctx.prec = integer_digits + fractional_digits + len(str(len(values))) + 2
        total = sum(values, Decimal(0))
    if total.is_zero():
        return "0"
    text = format(total, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def summarize_timetable_credits(items, *, complete: bool, has_more: bool,
                                issues: list) -> dict:
    """Count each course code once, preserving missing and conflicting credits.

    ``all_visible_credits_known`` concerns the provided rows only. It does not
    establish source completeness or include enrollment absent from a timetable.
    Only course codes and generic caveats enter this summary; issue messages,
    student identifiers, teacher names and other source fields are not copied.
    """
    courses = {}
    unidentified = 0
    for row in items:
        code = row.get("course_code") if isinstance(row, dict) else None
        if not isinstance(code, str) or not code.strip():
            unidentified += 1
            continue
        code = code.strip()
        group = courses.setdefault(code, {"values": set(), "unknown": False})
        value = _credit(row.get("credit"))
        if value is None:
            group["unknown"] = True
        else:
            group["values"].add(value)

    unknown, conflicting, known = [], [], []
    for code, group in courses.items():
        if group["unknown"]:
            unknown.append(code)
        if len(group["values"]) > 1:
            conflicting.append(code)
        if not group["unknown"] and len(group["values"]) == 1:
            known.append(next(iter(group["values"])))

    issue_codes = {issue.get("code") for issue in issues if isinstance(issue, dict)
                   and isinstance(issue.get("code"), str)}
    omitted_practical = "adjacent_data_omitted" in issue_codes
    unproven = "completeness_unproven" in issue_codes
    notes = []
    if not complete or unproven:
        notes.append("来源完整性未证；仅汇总当前课表可见课程，不代表本学期全部修读学分。")
    if has_more:
        notes.append("仍有未读分页；本汇总只覆盖传入条目。")
    if omitted_practical:
        notes.append("来源另有未包含的实践课块，未计入本汇总。")
    if unknown:
        notes.append("部分课程缺少有效学分，已从已知学分合计中排除，不能按零学分解释。")
    if conflicting:
        notes.append("同课程号存在学分冲突，相关课程未计入已知学分合计。")
    if unidentified:
        notes.append("部分课表行缺少有效课程号，无法可靠去重，已单独计数且未累计学分。")
    if not courses:
        notes.append("当前快照没有可汇总的已识别课程，不能据此断言本人未修课。")

    return {
        "aggregation_scope": "timetable_visible_courses",
        "visible_course_count": len(courses),
        "known_credit_sum": _exact_sum(known),
        "unknown_credit_courses": sorted(unknown),
        "conflicting_courses": sorted(conflicting),
        "unidentified_row_count": unidentified,
        "all_visible_credits_known": not (unknown or conflicting or unidentified),
        "source_complete": bool(complete and not has_more and not omitted_practical and not unproven),
        "notes": notes,
    }
