# -*- coding: utf-8 -*-
"""业务数据模型（工具输出的包装契约）。

这些 TypedDict 定义同时用于：
1. MCP 工具返回注解 Annotated[CallToolResult, <模型>] → 由 SDK 生成 outputSchema；
2. 运行期校验：SDK 在工具返回非错误结果时用该模型校验 structured_content
   （见 .venv/Lib/site-packages/mcp/server/mcpserver/utilities/func_metadata.py
    的 convert_result 分支：`if output_model is not None and not result.is_error`）。

约定（对应 docs/技术设计与Agent交接.md 第 8 节）：
- 所有键始终存在（缺省值为 None / [] / False），避免下游对缺键做默认值猜测；
- 时间字段为带时区的 ISO 8601 字符串（本机固定 +08:00——中国无夏令时，
  以固定偏移免装 tzdata；显示与日切均按 Asia/Shanghai）；
- complete 仅在「读完该来源、该查询范围的全部页或有明确结束证据」时为 true；
  当前课表来源无总数/结束证据，始终为 false，并以 completeness_unproven
  issue 说明（对应审阅要求「不自动将 coverage.complete 置为 true」）。
"""
from __future__ import annotations

from typing import NotRequired, Literal, TypedDict

Status = Literal["ok", "partial", "unavailable"]
Consistency = Literal["source_snapshot", "best_effort", "unknown"]

MODULE_TIMETABLE = "campus.timetable"
SOURCE_COURSE_API = "src.course-api"
MODULE_EXAMS = "campus.exams"
MODULE_GRADES = "campus.grades"
MODULE_GRADE_DETAILS = "campus.grade_details"
MODULE_INTERNSHIP = "campus.internship"
MODULE_ACADEMIC = "campus.academic"
MODULE_TRAINING_PLAN = "campus.training_plan"
MODULE_TRAINING_PLAN_DOC = "campus.training_plan_doc"
MODULE_TEACHING_PLAN = "campus.teaching_plan"
MODULE_TEACHING_PLAN_COURSES = "campus.teaching_plan_courses"
MODULE_RETAKE_COURSES = "campus.retake_courses"
MODULE_ACADEMIC_WARNINGS = "campus.academic_warnings"
MODULE_CREDIT_REQUIREMENTS = "campus.credit_requirements"
MODULE_MY_PROFILE = "campus.my_profile"
MODULE_ONLINE_URLS = "campus.online_urls"
MODULE_SYLLABUS = "campus.syllabus"
MODULE_INNOVATION = "campus.innovation"
MODULE_TEACHER_PAGES = "teachers.pages"
MODULE_FREE_CLASSROOMS = "campus.free_classrooms"
MODULE_CLASS_TIMETABLE = "campus.class_timetable"
MODULE_NOTICES = "notices.public"
MODULE_JOB_FAIRS = "campus.job_fairs"
MODULE_CAREER_INFO = "career.info"
MODULE_CAMPUS_NEWS = "news.campus"
MODULE_LIBRARY_INFO = "library.info"
MODULE_EXAM_REGISTRATIONS = "exams.registrations"
MODULE_APP_MESSAGES = "app.messages"
MODULE_COURSE_CATALOG = "course.catalog"
MODULE_PRESELECT_INFO = "course.preselect"
MODULE_LIBRARY_SEARCH = "library.search"
MODULE_JWW_NOTICES = "jww.notices"
MODULE_FITNESS_SCORES = "fitness.scores"
MODULE_FITNESS_SCHEDULE = "fitness.schedule"
MODULE_EVALUATION_RESULT = "evaluation.result"
MODULE_LABOR_COURSES = "labor.courses"
MODULE_LABOR_COURSE_DETAIL = "labor.course_detail"
MODULE_HANDBOOK_STUDENT = "handbook.student"
MODULE_TIMETABLE_WEB = "campus.timetable_web"
SOURCE_JWGLXT = "src.jwglxt"
MODULE_EVENTS = "campus.events"
SOURCE_JYW_XJH = "src.jyw-xjh"
ACCOUNT_PUBLIC = "public"  # 公开来源的账号域（对应交接文档第 7 节「公共来源使用明确的 public 域」）
# T1-T3（2026-10-16）：F11 审批进度 / F14 奖助公示 / F26 数字资源
MODULE_APPROVALS = "approvals.mine"
MODULE_AWARDS_PUBLICATIONS = "awards.publications"
MODULE_DIGITAL_RESOURCES = "library.digital"
SOURCE_FLOW = "src.flow"
SOURCE_XSC_PUB = "src.xsc"
SOURCE_LIB = "src.lib"


class Issue(TypedDict):
    code: str
    retryable: bool
    safe_message: str
    # F29：认证域标记——"independent" 表示独立凭据（体测 token 等），
    # 统一认证自动重登无法修复；缺省（无该键）= unified 域。
    auth_domain: NotRequired[str]
    # F29-R6：结构化恢复信号——"app_session" 仅由服务层按来源/端点/业务码/
    # 本机会话条件设置；恢复判定只认该字段，不解析提示文本。
    auth_hint: NotRequired[str]


class SourceRef(TypedDict):
    source_id: str
    safe_entry_url: str
    fetched_at: str | None
    parser_version: str
    evidence_ref: str


class SourceSnapshot(TypedDict):
    source_id: str
    query_scope: str
    snapshot_id: str | None
    complete: bool
    consistency: Consistency
    missing_ranges: list[str]


class Coverage(TypedDict):
    source_snapshots: list[SourceSnapshot]


class Pagination(TypedDict):
    has_more: bool
    next_cursor: str | None
    result_snapshot_id: str | None


class SourceFreshness(TypedDict):
    source_id: str
    last_attempt_at: str | None
    last_success_at: str | None
    data_age_seconds: int | None
    is_stale: bool | None


class Freshness(TypedDict):
    last_attempt_at: str | None
    last_success_at: str | None
    data_age_seconds: int | None
    is_stale: bool | None
    by_source: list[SourceFreshness]
    cache_hit: NotRequired[bool]
    cache_ttl_seconds: NotRequired[int]
    cache_remaining_seconds: NotRequired[int]


class TimetableEntry(TypedDict):
    """课表条目（字段以来源实际提供为准；不猜周次集合与节次时刻）。

    映射来源：studentCourse 回包白名单字段（见代码链报告 4.2 节）。
    - weekday: xqj，1=周一 … 7=周日
    - period_start/period_end: ksj/jsj（客户端节次，具体时刻由客户端硬编码，
      不随数据返回；本对象不包含时刻）
    - week_no: zc（来源以单值返回；保留原始类型 int 或 str）
    - date: nyr（yyyy-MM-dd）；date_short: skrq（MM-dd）
    - location: jsmc；teacher_id: jsbh（语义待核实：R22——网页版已实证 jgh=工号/xm=姓名，App jsbh 未经同记录对照，不得当工号映射姓名或跨源替换）
    """
    entry_id: str
    course_name: str
    course_code: str | None
    course_type: str | None
    weekday: int
    period_start: int
    period_end: int
    week_no: int | str | None
    date: str
    date_short: str | None
    location: str | None
    teacher_id: str | None
    academic_year: str | None
    semester: str | None
    source_id: str


class TimetableObservation(TypedDict):
    status: Status
    module_id: str
    items: list[TimetableEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class CampusEvent(TypedDict):
    """招聘宣讲会条目（公开来源：智慧就业平台；字段以来源实际提供为准）。

    - event_id: xjhid（来源业务 ID）；title: xjhzt（主题）；company: dwmc（单位）
    - date: jbrq（yyyy-MM-dd，举办日）；time_range/start_time/end_time: jbsjdmc 解析
    - campus: jbxqmc（校区）；location: 校内=cdmc / 线上=xsxjhwz
    - format: jbxsmc（如「校内宣讲」）；cancelled: shzt=='05'（与页面同构规则）
    - status 为响应时按当前时间计算（不写入缓存）：
      已取消 / 进行中 / 未开始 / 已结束；无法计算为 None
    - url: 公开详情页（无票据）；view_count: djs；on_top: ontop=='是'
    """
    event_id: str
    title: str
    company: str
    event_type: str
    date: str
    time_range: str | None
    start_time: str | None
    end_time: str | None
    campus: str | None
    location: str | None
    format: str | None
    cancelled: bool
    status: str | None
    company_type: str | None
    industry: str | None
    company_size: str | None
    positions: str | None
    view_count: int | None
    on_top: bool
    url: str
    source_id: str


class EventsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[CampusEvent]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class CapabilityEntry(TypedDict):
    module_id: str
    label: str
    menu_hint: str
    data_scope: str
    evidence_state: str
    adapter_state: str
    tools: list[str]
    gaps: list[str]
    notes: str


class CapabilitiesReport(TypedDict):
    status: Status
    generated_at: str
    coverage_note: str
    modules: list[CapabilityEntry]


class SessionHealth(TypedDict):
    present: bool
    user_type: str | None
    created_at: str | None


class SourceHealthRow(TypedDict):
    account_scope_id: str
    source_id: str
    module_id: str
    last_attempt_at: str | None
    last_success_at: str | None
    data_age_seconds: int | None
    is_stale: bool | None
    last_issue_code: str | None
    last_issue_message: str | None
    last_attempt_query_scope: str | None


class HealthReport(TypedDict):
    status: Status
    generated_at: str
    account_scope_id: str | None
    session: SessionHealth
    sources: list[SourceHealthRow]
    issues: list[Issue]


# ---------------- 教务网页版（jwglxt）：考试/成绩/网页课表 ----------------

class ExamEntry(TypedDict):
    """考试条目（字段映射见 adapters/campus/jwweb.py _EXAM_FIELDS；口径以来源为准）。"""
    entry_id: str
    exam_name: str | None
    course_name: str
    course_code: str | None
    exam_datetime_text: str | None   # kssj 来源原文（如 "2026-07-01(08:40-10:40)"，不拆分不猜测）
    weekly_slots_text: str | None    # sksj 来源原文（课程原排课时间，如 "星期二第3-5节{1-16周}"）
    room: str | None
    room_type: str | None
    exam_mode: str | None            # 闭卷/上机等（原文）
    class_name: str | None
    school: str | None
    semester_code: str | None
    paper_no: str | None
    source_id: str


class GradeEntry(TypedDict):
    """成绩条目（cj/jd/xf/jsxm 等来源字段白名单映射；教师为分号分隔原文）。"""
    entry_id: str
    course_name: str
    course_code: str | None
    score: str | None
    grade_point: str | None
    credit: str | None
    teachers_text: str | None
    teaching_class: str | None
    course_type: str | None
    assessment_mode: str | None
    course_category: str | None
    academic_year: str | None
    semester: str | None
    source_id: str


class TimetableWebEntry(TypedDict):
    """网页课表条目（与原生 get_timetable 并存：本对象来自教务网页版，含教师工号+姓名）。"""
    entry_id: str
    course_name: str
    course_code: str | None
    teaching_class: str | None
    period_text: str | None          # 节次原文（如 "3-5节"）
    period_span: str | None
    weekday: str | None
    weekday_text: str | None
    weeks_text: str | None           # 周次原文（如 "1-2周,4-15周"）
    room: str | None
    teacher_id: str | None           # jgh（教师工号；已实证 02242 类代码是教室而非教师）
    teacher_name: str | None
    credit: str | None
    course_type: str | None
    course_category: str | None
    enrolled: str | None
    class_list: str | None
    academic_year: str | None
    semester: str | None
    source_id: str


class GradeComponent(TypedDict):
    """成绩组成项（xmblmc 原文含权重，如 "平时(50%)"；xmcj 为该项得分）。"""
    component_name: str
    component_score: str | None


class GradeDetailEntry(TypedDict):
    """单门课程的成绩明细（仅来源提供明细的课程；无明细课程不出现在来源列表）。"""
    entry_id: str
    course_name: str
    course_code: str | None
    teaching_class: str | None
    total_score: str | None          # zpcj 总评原文
    school: str | None
    credit: str | None
    components: list[GradeComponent]  # 项目组成；取得失败时为空并以 issues 说明
    components_failed: bool           # True=该项目成绩读取失败（具体见 issues）
    academic_year: str | None
    semester: str | None
    source_id: str


class InternshipGradeEntry(TypedDict):
    """实习成绩条目（N544030；本人各学期实测合法空——无记录时 items=[] 为真实结果）。"""
    entry_id: str
    course_name: str
    course_code: str | None
    final_score: str | None
    teacher: str | None
    project_name: str | None
    campus: str | None
    teaching_class: str | None
    group_name: str | None
    academic_year: str | None
    semester: str | None
    source_id: str


class InternshipObservation(TypedDict):
    status: Status
    module_id: str
    items: list[InternshipGradeEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class AcademicProgressEntry(TypedDict):
    """学业情况统计（N105515 服务端渲染页解析；全部为来源原文，不做毕业资格推导）。

    课程级列表（已修/在修/未修标签）为前端渲染、无独立数据接口——本条目不含，
    缺口由 issues 登记。
    """
    entry_id: str
    gpa: str | None
    planned_courses: str | None      # 计划总课程门数
    passed: str | None               # 计划内通过门数
    failed: str | None               # 计划内未通过门数
    not_started: str | None          # 未修门数
    in_progress: str | None          # 在读门数
    extra_passed: str | None         # 计划外通过门数
    extra_failed: str | None         # 计划外未通过门数
    stat_time_text: str | None       # 来源统计时间原文（"…之前有效"）
    raw_text: str | None             # 统计块原文（截断保存，便于核对）
    source_id: str


class AcademicProgressObservation(TypedDict):
    status: Status
    module_id: str
    items: list[AcademicProgressEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class TrainingPlanEntry(TypedDict):
    """培养方案文件（N153045，全校文件库按适用年级过滤；plan_ref 供附件下载后续接入）。"""
    entry_id: str
    plan_name: str                    # 如 "2024示例专业"
    plan_ref: str | None
    version: str | None               # 版本号（如 2023）
    apply_grades: str | None          # 适用年级列表原文（逗号分隔）
    school: str | None
    major_name: str | None
    major_code: str | None
    total_credit: str | None          # 总学分原文
    has_attachment: str | None        # 来源附件标志（"1"/"0"）
    plan_kind: str | None             # 专业/大类
    source_id: str


class TrainingPlanObservation(TypedDict):
    status: Status
    module_id: str
    items: list[TrainingPlanEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class TrainingPlanDocEntry(TypedDict):
    """培养方案附件正文下载结果（pyfaxxwh 端点，2026-10-18 PC 会话实测打通）。

    文件落盘本机 data/training_plan_docs/；docx 为官方原件字节，txt 为
    python-docx 提取的段落+表格全文（便于检索引用；缺 python-docx 时为 None）。"""
    entry_id: str
    kind: str                         # "training_plan_doc"（本次）/ "stale_file"（保旧）
    plan_name: str                    # 如 "2024示例专业"（pyfamc 原文）
    apply_grade: str                  # 适用年级（synj 过滤值）
    version: str | None               # 版本号（如 2023）
    total_credit: str | None          # 总学分原文
    file_path: str                    # 落盘 docx 绝对路径
    size_bytes: int
    content_type: str
    text_path: str | None             # 提取全文 txt 绝对路径（可选依赖 python-docx）
    text_chars: int | None
    text_preview: str | None          # 全文前 400 字
    downloaded_at: str
    source_id: str
    sha256: str | None                # 落盘 docx 的 SHA-256（保旧校验用）
    text_complete: bool | None        # 全文提取是否完整（False 时不得称可靠全文）


class TrainingPlanDocObservation(TypedDict):
    status: Status
    module_id: str
    items: list[TrainingPlanDocEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class TeachingPlanEntry(TypedDict):
    """教学执行计划条目（N153540，按年级列各专业计划概览；课程明细为独立接口待后续）。"""
    entry_id: str
    plan_ref: str | None              # jxzxjhxx_id（课程明细查询的行标识，后续接入）
    grade: str | None
    major_name: str | None
    major_code: str | None
    total_credit: str | None
    course_count: str | None          # 计划课程数（来源原文）
    campus: str | None
    school_code: str | None
    planned_seats: str | None
    plan_kind: str | None
    is_higher_vocational: str | None
    source_id: str


class TeachingPlanObservation(TypedDict):
    status: Status
    module_id: str
    items: list[TeachingPlanEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class TeachingPlanCourseEntry(TypedDict):
    """执行计划单计划课程明细（jxzxjhkcxx；plan_ref 来自 get_teaching_plans）。"""
    entry_id: str
    course_code: str | None
    course_name: str
    course_type: str | None
    course_category: str | None
    credit: str | None
    campus: str | None
    credit_node_name: str | None     # 学分要求节点名（如 "软件工程与数据科学初级课程"）
    hours_text: str | None           # 学时原文（如 "理论(2.0)-实验(2.0)"）
    term_year: str | None
    term_code: str | None
    school: str | None
    source_id: str


class TeachingPlanCoursesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[TeachingPlanCourseEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class RetakeCourseEntry(TypedDict):
    """重修课程条目（N255720，全校重修开课；按重修开设学年学期过滤）。"""
    entry_id: str
    course_code: str | None
    course_name: str
    course_category: str | None
    school: str | None
    credit: str | None
    retake_year: str | None          # 重修学年原文（如 2026-2027）
    retake_term: str | None
    retake_status: str | None        # 重修状态原文
    source_id: str


class RetakeCoursesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[RetakeCourseEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class AcademicWarningEntry(TypedDict):
    """学业预警状态（N305516 服务端渲染页；原文呈现，有预警时列表结构以原文为准）。"""
    entry_id: str
    has_warning: bool                 # False=页面明示「暂无预警」
    summary_text: str | None          # 预警状态原文（截断保存）
    source_id: str


class AcademicWarningsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[AcademicWarningEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class CreditRequirementNode(TypedDict):
    """培养方案学分要求节点（N105505 树；一个节点=一类学分要求，含其课程池）。"""
    node_name: str                     # 学分要求节点名（如 "程序设计基础课程"）
    courses: list[dict]                # 节点下课程（原文白名单：课程/学期/学分/已得学分）
    course_count: int
    source_id: str


class CreditRequirementsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[CreditRequirementNode]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class MyProfileEntry(TypedDict):
    """本人基本资料（N100801 服务端渲染页；字段名为教务页面原文 label，含个人敏感信息）。"""
    entry_id: str
    profile: dict                     # 原文 label→值（31 字段实测：学号/姓名/专业/班级/学籍状态等）
    source_id: str


class MyProfileObservation(TypedDict):
    status: Status
    module_id: str
    items: list[MyProfileEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class OnlineUrlEntry(TypedDict):
    """网上上课地址条目（N1598；online_url 为来源原文，认证凭据不进输出）。"""
    entry_id: str
    course_code: str | None
    course_name: str
    teacher: str | None
    teacher_id: str | None
    teaching_class: str | None
    location_text: str | None
    class_time_text: str | None
    online_url: str | None            # 来源原文地址（官方入口）
    attachment_path: str | None
    attachment_name: str | None
    group_number: str | None
    credit: str | None
    enrolled: str | None
    campus: str | None
    academic_year: str | None
    semester: str | None
    course_type: str | None
    course_category: str | None
    course_format: str | None
    source_id: str


class OnlineUrlsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[OnlineUrlEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class SyllabusEntry(TypedDict):
    """授课提纲条目（N155002；syllabus_id 供正文查询后续接入）。"""
    entry_id: str
    course_code: str | None
    course_name: str
    teacher: str | None
    teaching_class: str | None
    teaching_class_id: str | None
    teaching_group: str | None
    syllabus_id: str | None
    course_type: str | None
    open_kind: str | None
    school: str | None
    is_arranged: str | None
    academic_year: str | None
    semester: str | None
    syllabus_status: str | None        # rlztmc 提纲状态（保存/待审核/已发布）
    syllabus_status_code: str | None   # rlzt 状态码
    source_id: str


class SyllabusObservation(TypedDict):
    status: Status
    module_id: str
    items: list[SyllabusEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class InnovationScoreEntry(TypedDict):
    """创新创业获得分值条目（N4780；字段映射按真实样本精确化 2026-10-15）。"""
    entry_id: str
    project_name: str | None           # xmlbmc 项目类别名（创新创业学分/素质拓展学分）
    score: str | None                  # hdfz 获得分值（数字，0 合法）
    max_require: str | None            # zgfzyq 最高分值要求
    completed: str | None              # sjyxfz 是否已修分值
    grade: str | None                  # cj 成绩等级（及格/不及格）
    swap_support: str | None           # thfzcz 置换支持
    student_id: str | None             # xh_id（本人，账号域内）
    source_id: str


class InnovationScoresObservation(TypedDict):
    status: Status
    module_id: str
    items: list[InnovationScoreEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class TeacherPageEntry(TypedDict):
    """教师主页条目（mypage 公开源；photo_url 已 R41 净化）。"""
    entry_id: str
    name: str                          # 教师姓名（来源 title 字段）
    page_url: str | None               # 主页 URL（净化后官方入口）
    career: str | None                 # 职称
    department: str | None             # 院系单位
    education: str | None              # 学历
    own_department: str | None
    discipline: str | None             # 学科
    visit_count: str | None
    photo_url: str | None              # 照片 URL（净化后）
    home_text: NotRequired[str | None]  # 主页正文原文（detail=true 有值；列表/旧快照 None）
    source_id: str


class TeacherPagesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[TeacherPageEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class FreeClassroomEntry(TypedDict):
    """空闲教室条目（N2155；字段为来源原文白名单，具体键按在线样本后核对）。"""
    entry_id: str
    raw: dict                          # 来源行原文（字段清单以实际响应为准）
    source_id: str


class FreeClassroomsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[FreeClassroomEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class CampusServiceEntry(TypedDict):
    """校园服务条目（服务中心目录，公开查询型）。"""
    entry_id: str
    service_name: str                  # fwmc 服务名
    category: str | None               # fwlbmcs 类别
    handling_mode: str | None          # blmsmc 办理方式（线上/线下）
    entry_url: str | None              # fwdz 入口（R41 净化后官方地址）
    handling_count: str | None         # blcs 办理量
    rating: str | None                 # fwpf 评分
    publish_status: str | None         # fblxmc 发布状态
    source_id: str


class CampusServicesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[CampusServiceEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class NoticeEntry(TypedDict):
    """公开通知条目（学校新闻网，匿名可读；不替代个人收件箱）。"""
    entry_id: str
    title: str                         # 通知标题
    url: str | None                    # 通知详情 URL
    date: str | None                   # 发布日期（yyyy-MM-dd）
    detail_text: NotRequired[str | None]  # 通知正文（detail_url 有值；列表/旧快照 None）
    source_id: str


class NoticesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[NoticeEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class ClassTimetableEntry(TypedDict):
    """班级课表条目（kbcx 模块 bh_id 维度；字段为来源原文白名单）。"""
    entry_id: str
    course_code: str | None            # kch
    course_name: str                   # kcmc
    teaching_class: str | None         # jxbmc
    teacher: str | None                # xm
    teacher_id: str | None             # jgh
    location: str | None               # cdmc
    weeks: str | None                  # zcd 周次原文
    section: str | None                # jc 节次原文
    weekday: str | None                # xqj 星期几
    class_style: str | None            # kcbj（主修等）
    credit: str | None                 # xf
    class_nature: str | None           # kcxz
    source_id: str


class ClassTimetableObservation(TypedDict):
    status: Status
    module_id: str
    items: list[ClassTimetableEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class JobFairEntry(TypedDict):
    """招聘会场次条目（jyw 智慧就业平台，匿名公开源）。"""
    entry_id: str
    title: str                         # zphzt 场次名称
    fair_type: str | None              # zphlbmc（大型/中型招聘会等）
    start_time: str | None             # jbkssj 举办开始（原文）
    end_time: str | None               # jbjssj 举办结束（原文；**≠实际线下结束，以详情页为准**）
    location: str | None               # jbdd 举办地点
    signup_deadline: str | None        # bmjzsj 报名截止（单位报名，非学生入场）
    invited_orgs: str | None           # bmdws 邀请单位数
    fair_id: str | None                # zphid
    detail_text: NotRequired[str | None]  # 邀请函正文（detail_id 有值；列表/旧快照 None）
    source_id: str


class JobFairsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[JobFairEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class CareerInfoEntry(TypedDict):
    """智慧就业网栏目条目（公开源；zpxx 系含岗位字段，news 系含文章字段）。"""
    entry_id: str
    category_label: str                # 栏目中文标签
    title: str                         # 标题（岗位简章/文章标题）
    entry_url: str | None              # 详情 URL（R41 净化后）
    deadline: str | None               # 岗位截止日期（zpxx 系）
    demand: str | None                 # 需求人数（zpxx 系）
    unit: str | None                   # 单位
    address: NotRequired[str | None]   # 工作地点（zpxx 系）
    job_id: NotRequired[str | None]    # zpxxid 岗位标识
    source_id: str


class CareerInfoObservation(TypedDict):
    status: Status
    module_id: str
    items: list[CareerInfoEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class CampusNewsEntry(TypedDict):
    """学校新闻网条目（匿名公开源）。"""
    entry_id: str
    title: str | None                  # 详情路径 URL 对应标题未知 → None
    url: str | None
    date: str | None
    category_label: str | None
    detail_text: NotRequired[str | None]
    source_id: str


class CampusNewsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[CampusNewsEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class LibraryInfoEntry(TypedDict):
    """图书馆公开信息条目（馆藏布局原文/栏目条目）。"""
    entry_id: str
    title: str | None
    url: str | None
    date: str | None
    text: str | None
    category_label: str | None
    source_id: str


class LibraryInfoObservation(TypedDict):
    status: Status
    module_id: str
    items: list[LibraryInfoEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class ExamRegistrationEntry(TypedDict):
    """考级项目报名状态条目（学生端本人视角；空=当前无报名记录）。"""
    entry_id: str
    project_name: str | None           # 报名项目名称（四六级/计算机等级等）
    status: str | None                 # 报名/审核状态（原文）
    signup_time: str | None            # 报名时间（原文）
    exam_date: str | None              # 考试时间（原文，如可读）
    source_id: str


class ExamRegistrationsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[ExamRegistrationEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class AppMessageEntry(TypedDict):
    """门户消息/通知条目（App 域本人视角）。"""
    entry_id: str
    title: str | None
    content: str | None
    date: str | None
    url: str | None
    source_id: str


class AppMessagesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[AppMessageEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class CourseCatalogEntry(TypedDict):
    """全校课程信息条目（B01-03；课程号/名称/学分/学时/类别/开课学院）。"""
    entry_id: str
    course_code: str | None           # kch 课程号
    course_name: str                  # kcmc 课程名称
    course_name_en: str | None        # kcywmc 英文名称
    credit: str | None                # xf 学分
    total_hours: str | None           # zxs 总学时
    hours_detail: str | None          # xsxxxx 学时构成（理论-实验-实习）
    course_category: str | None       # kclbmc 课程类别
    school: str | None                # kkbmmc 开课学院
    course_key: str | None            # kch_id 课程唯一标识
    source_id: str


class CourseCatalogObservation(TypedDict):
    status: Status
    module_id: str
    items: list[CourseCatalogEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class PreselectInfoEntry(TypedDict):
    """学生预选信息条目（B01-04；本人预选课程记录）。"""
    entry_id: str
    course_code: str | None           # kch 课程号
    course_name: str                  # kcmc 课程名称
    credit: str | None                # xf 学分
    student_id: str | None            # xh 学号
    student_name: str | None          # xm 姓名
    school: str | None                # xy 学院
    major: str | None                 # zy 专业
    grade: str | None                 # nj 年级
    academic_year: str | None
    semester: str | None
    source_id: str


class PreselectInfoObservation(TypedDict):
    status: Status
    module_id: str
    items: list[PreselectInfoEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class LibrarySearchEntry(TypedDict):
    """馆藏检索书目条目（superlib 聚合，匿名公开源）。"""
    entry_id: str
    title: str | None                  # 书名/资源名
    url: str | None                    # 详情链接
    keyword: str | None                # 检索关键字
    detail_text: NotRequired[str | None]  # 书目详情正文（detail_url 时返回）
    isbn: NotRequired[str | None]      # ISBN
    clc_no: NotRequired[str | None]    # 中图法分类号
    price: NotRequired[str | None]     # 定价
    publish: NotRequired[str | None]   # 出版项
    source_id: str


class LibrarySearchObservation(TypedDict):
    status: Status
    module_id: str
    items: list[LibrarySearchEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class JwwNoticeEntry(TypedDict):
    """教务处事务通知条目/详情（F06，匿名公开源）。"""
    entry_id: str
    title: str | None
    url: str | None
    date: str | None
    detail_text: NotRequired[str | None]
    current_week: NotRequired[str | None]     # 当前教学周（详情模式解析）
    semester_start: NotRequired[str | None]   # 学期开始日期
    source_id: str


class JwwNoticesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[JwwNoticeEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class FitnessScoreEntry(TypedDict):
    """体测成绩条目（F17，本人视角单次记录）。"""
    entry_id: str
    exam: str | None                   # 体测学年
    grade: str | None                  # 年级
    gender: str | None
    height: str | None
    weight: str | None
    bmi: str | None
    vital_capacity: str | None         # 肺活量
    fifty_meter: str | None            # 50 米
    standing_long_jump: str | None     # 立定跳远
    sit_and_reach: str | None          # 坐位体前屈
    pull_up: str | None                # 引体向上
    thousand_meter: str | None         # 1000 米
    score: str | None                  # 总分
    rank: str | None                   # 等级
    student_no: str | None
    name: str | None
    source_id: str


class FitnessScoresObservation(TypedDict):
    status: Status
    module_id: str
    items: list[FitnessScoreEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class EvaluationResultEntry(TypedDict):
    """综合测评结果条目（F13，本人视角；周期外如实 unavailable）。"""
    entry_id: str
    source_id: str


class EvaluationResultObservation(TypedDict):
    status: Status
    module_id: str
    items: list[EvaluationResultEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class FitnessScheduleEntry(TypedDict):
    """体测预约时段条目（F17 扩展；只读，不代预约）。"""
    entry_id: str
    exam_name: str | None              # 学年名称
    exam_year: str | None
    subscribe_begin: str | None        # 预约开始时间
    subscribe_end: str | None          # 预约截止
    exam_begin: str | None             # 体测开始
    exam_end: str | None               # 体测截止
    morning_begin: str | None
    morning_end: str | None
    afternoon_begin: str | None
    afternoon_end: str | None
    enabled: str | None
    source_id: str


class FitnessScheduleObservation(TypedDict):
    status: Status
    module_id: str
    items: list[FitnessScheduleEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class LaborCourseEntry(TypedDict):
    """劳动教育课程项目条目（F16，本人视角首屏）。"""
    entry_id: str
    project_id: str | None
    project_name: str | None           # 项目名称
    dept_names: str | None             # 学院限制
    start_time: str | None             # 开始时间
    end_time: str | None
    apply_num: str | None              # 已报名人数
    residue: str | None                # 剩余名额
    project_type: str | None           # 分类
    is_apply: str | None               # 是否已报
    address: str | None
    source_id: str


class LaborCoursesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[LaborCourseEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class LaborCourseDetailEntry(TypedDict):
    """劳动课程详情+本人记录条目（B03；字段按来源核实，未知不透传）。"""
    entry_id: str
    project_id: str | None
    projectName: NotRequired[str | None]
    deptNames: NotRequired[str | None]
    startTime: NotRequired[str | None]
    endTime: NotRequired[str | None]
    address: NotRequired[str | None]
    applyNum: NotRequired[str | None]
    residue: NotRequired[str | None]
    projectType: NotRequired[str | None]
    content: NotRequired[str | None]
    mine_courseName: NotRequired[str | None]
    mine_startTime: NotRequired[str | None]
    mine_endTime: NotRequired[str | None]
    mine_status: NotRequired[str | None]
    mine_times: NotRequired[str | None]
    mine_completedTimes: NotRequired[str | None]
    mine_teacherName: NotRequired[str | None]
    mine_place: NotRequired[str | None]
    source_id: str


class LaborCourseDetailObservation(TypedDict):
    status: Status
    module_id: str
    items: list[LaborCourseDetailEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class HandbookEntry(TypedDict):
    """学生手册条目（B03-③；2026 版学生处公开 PDF）。"""
    entry_id: str
    page: str | None                   # 命中页码（keyword 模式）
    context: str | None                # 目录或命中上下文
    version: NotRequired[str | None]   # 手册版本（2026）
    source_id: str


class HandbookObservation(TypedDict):
    status: Status
    module_id: str
    items: list[HandbookEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class ExamsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[ExamEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class GradeDetailsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[GradeDetailEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class GradesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[GradeEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class TimetableCreditSummary(TypedDict):
    aggregation_scope: Literal["timetable_visible_courses"]
    visible_course_count: int
    known_credit_sum: str
    unknown_credit_courses: list[str]
    conflicting_courses: list[str]
    unidentified_row_count: int
    all_visible_credits_known: bool
    source_complete: bool
    notes: list[str]


class RequestTiming(TypedDict):
    method: str
    elapsed_ms: float
    http_status: int | None
    outcome: str


class SourceQueryTiming(TypedDict):
    mode: Literal["network_attempt", "local_snapshot", "local_rejection"]
    total_ms: float
    context_ms: float
    http_ms: float
    parse_ms: float
    local_result_ms: float
    http_request_count: int
    requests: list[RequestTiming]


class CurrentCallTiming(TypedDict):
    handler_ms: float
    http_request_count: int


class TimetablePerformance(TypedDict):
    source_query: SourceQueryTiming
    current_call: NotRequired[CurrentCallTiming]


class TimetableWebObservation(TypedDict):
    status: Status
    module_id: str
    items: list[TimetableWebEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]
    credit_summary: NotRequired[TimetableCreditSummary]
    performance: NotRequired[TimetablePerformance]


# ---------------- T1-T3（2026-10-16）：F11/F14/F26 条目与观测 ----------------


class ApprovalRequestEntry(TypedDict):
    """本人流程申请条目（F11；泛微 Ecology OA「我的请求/待办/已办」视图）。"""
    entry_id: str
    request_id: str | None            # 流程 requestid（来源主键）
    request_name: str | None          # 流程标题原文
    create_date: str | None           # 创建日期（来源 yyyy-MM-dd）
    workflow_id: str | None           # 工作流数字 id（本视图不回工作流名）
    view_scope: str                   # mine / doing / done
    source_id: str


class ApprovalsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[ApprovalRequestEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class PublicationEntry(TypedDict):
    """学生处公开公示条目（F14；评奖评优/资助评审公示）。"""
    entry_id: str
    title: str | None                 # 公示标题
    url: str | None                   # 详情链接（净化后）
    publish_date: str | None          # 发布日期（列表页文本）
    text: NotRequired[str | None]     # detail_url 模式：正文原文（≤6000 字）
    attachments: NotRequired[list]    # detail_url 模式：附件（标题+链接）
    source_id: str


class PublicationsObservation(TypedDict):
    status: Status
    module_id: str
    items: list[PublicationEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class DigitalResourceEntry(TypedDict):
    """图书馆数字资源条目（F26；公开静态导航）。"""
    entry_id: str
    name: str | None                  # 资源名
    category: str | None              # 子栏目（中文电子期刊等）
    entry_url: str | None             # 资源入口 URL
    intro_url: str | None             # 【资源简介】链接
    access_way: str | None            # 访问方式说明（内网 IP→校内/VPN；其余不猜）
    source_id: str


class DigitalResourcesObservation(TypedDict):
    status: Status
    module_id: str
    items: list[DigitalResourceEntry]
    coverage: Coverage
    pagination: Pagination
    freshness: Freshness
    source_refs: list[SourceRef]
    issues: list[Issue]


class LibrarySeatArea(TypedDict):
    """选座区域行（badge=空位/总数，已双样本核验）。"""
    lib_id: int
    name: str
    floor: str
    free: int
    total: int


class LibrarySeatsObservation(TypedDict, total=False):
    """图书馆选座余座最小观测契约（2026-10-18）。

    成功：status=ok + areas/by_floor + freshness(live, data_age_seconds=0)；
    失败保旧：status=partial + stale=true + as_of；首次失败：unavailable。
    """
    status: Status
    module_id: str
    query_time: str
    semantic: str
    areas: list[LibrarySeatArea]
    by_floor: dict
    rank_by_free_rate: list
    session_note: str
    manual_path: str
    freshness: dict
    issues: list
    gaps: list[str]
    reason: str
    hint: str
    as_of: str


class DormElectricObservation(TypedDict, total=False):
    """寝室电表最小观测契约：data 为余额/账单/日明细，freshness 区分实时与旧快照。"""
    status: Status
    module_id: str
    query_time: str
    semantic: str
    data: dict
    freshness: dict
    issues: list
    gaps: list[str]
    reason: str
    hint: str
    as_of: str


class MpArticlesObservation(TypedDict, total=False):
    """本机公众号采集库观测：覆盖及采集时间未知，分页跨调用非固定快照。"""
    status: Status
    module_id: str
    query_time: str
    data: dict
    coverage: dict
    pagination: dict
    freshness: dict
    issues: list
    gaps: list[str]
    reason: str
    hint: str
