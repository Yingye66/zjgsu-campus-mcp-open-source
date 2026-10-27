# 工具索引

本页对应 48 个注册工具（46 个业务查询、2 个辅助工具）。注册不代表所有来源在线可用。余座默认提供人工入口，完整状态见[当前成果](release-status.md)。

本页列出发布源码的注册签名。参数由 MCP 客户端根据提问填写；不需要手工记住所有名称。具体来源可用性见[限制文档](limitations.md)。

成绩、成绩明细、考试及两种课表支持可选 `force_refresh`；缓存行为见[查询提速](performance.md)。

`get_timetable_web`同时返回同快照`credit_summary`及`performance`，用于可见课程学分小计和分段耗时；无需新增工具或另查全校课程库。

### `get_academic_progress`

```python
get_academic_progress(cursor: str | None = None, limit: int = 50)
```

### `get_academic_warnings`

```python
get_academic_warnings(cursor: str | None = None, limit: int = 50)
```

### `get_campus_events`

```python
get_campus_events(start_date: str, end_date: str, event_type: str = EVENT_TYPE_XJH, keyword: str | None = None, cursor: str | None = None, limit: int = 50)
```

### `get_campus_news`

```python
get_campus_news(category: str = '3', detail_url: str | None = None, cursor: str | None = None, limit: int = 30)
```

### `get_campus_notices`

```python
get_campus_notices(detail_url: str | None = None, keyword: str | None = None, page: int = 1, cursor: str | None = None, limit: int = 50)
```

### `get_career_info`

```python
get_career_info(category: str, keyword: str | None = None, page: int = 1, cursor: str | None = None, limit: int = 20)
```

### `get_class_timetable`

```python
get_class_timetable(class_name: str | None = None, bh_id: str | None = None, xnm: str | None = None, xqm: str | None = None, cursor: str | None = None, limit: int = 100)
```

### `get_course_catalog`

```python
get_course_catalog(keyword: str | None = None, xnm: str | None = None, xqm: str | None = None, page: int = 1, cursor: str | None = None, limit: int = 20)
```

### `get_credit_requirements`

```python
get_credit_requirements(cursor: str | None = None, limit: int = 50)
```

### `get_digital_resources`

```python
get_digital_resources(category: str = 'all', keyword: str | None = None, cursor: str | None = None, limit: int = 50)
```

### `get_dorm_electric`

```python
get_dorm_electric(mode: str = 'summary', month: str | None = None)
```

### `get_evaluation_result`

```python
get_evaluation_result(cursor: str | None = None, limit: int = 20)
```

### `get_exam_registrations`

```python
get_exam_registrations(cursor: str | None = None, limit: int = 30)
```

### `get_exams`

```python
get_exams(xnm: str, xqm: str, cursor: str | None = None, limit: int = 50, force_refresh: bool = False)
```

### `get_fitness_schedule`

```python
get_fitness_schedule(cursor: str | None = None, limit: int = 20)
```

### `get_fitness_scores`

```python
get_fitness_scores(cursor: str | None = None, limit: int = 20)
```

### `get_free_classrooms`

```python
get_free_classrooms(dm: str, xqh_id: str = '2', qssj: str = '', jssj: str = '', lh: str = '', cdlb_id: str = '', cdmc: str = '', cursor: str | None = None, limit: int = 50)
```

### `get_grade_details`

```python
get_grade_details(xnm: str, xqm: str, course: str | None=None, cursor: str | None=None, limit: int=50, force_refresh: bool=False)
```

### `get_grades`

```python
get_grades(xnm: str, xqm: str, cursor: str | None=None, limit: int=50, force_refresh: bool=False)
```

### `get_innovation_scores`

```python
get_innovation_scores(cursor: str | None = None, limit: int = 50)
```

### `get_internship_grades`

```python
get_internship_grades(xnm: str, xqm: str, cursor: str | None = None, limit: int = 50)
```

### `get_job_fairs`

```python
get_job_fairs(detail_id: str | None = None, cursor: str | None = None, limit: int = 50)
```

### `get_jww_notices`

```python
get_jww_notices(detail_url: str | None = None, keyword: str | None = None, page: int = 1, cursor: str | None = None, limit: int = 20)
```

### `get_labor_course_detail`

```python
get_labor_course_detail(project_id: str, cursor: str | None = None, limit: int = 20)
```

### `get_labor_courses`

```python
get_labor_courses(cursor: str | None = None, limit: int = 20)
```

### `get_library_info`

```python
get_library_info(category: str = 'gzbj', cursor: str | None = None, limit: int = 30)
```

### `get_library_search`

```python
get_library_search(keyword: str = '', search_type: str = 'book', detail_url: str | None = None, cursor: str | None = None, limit: int = 20)
```

### `get_library_seats`

```python
get_library_seats()
```

### `get_my_approvals`

```python
get_my_approvals(scope: str = 'mine', cursor: str | None = None, limit: int = 20)
```

### `get_my_messages`

```python
get_my_messages(cursor: str | None = None, limit: int = 20)
```

### `get_my_profile`

```python
get_my_profile(cursor: str | None = None, limit: int = 50, fields: str | None = None, include_sensitive: bool = False)
```

### `get_online_class_urls`

```python
get_online_class_urls(xnm: str, xqm: str, cursor: str | None = None, limit: int = 50)
```

### `get_preselect_info`

```python
get_preselect_info(xnm: str | None = None, xqm: str | None = None, cursor: str | None = None, limit: int = 20)
```

### `get_retake_courses`

```python
get_retake_courses(cxxnm: str, cxxqm: str, keyword: str | None = None, cursor: str | None = None, limit: int = 50)
```

### `get_scholarship_publications`

```python
get_scholarship_publications(keyword: str | None = None, page: int = 1, detail_url: str | None = None, cursor: str | None = None, limit: int = 20)
```

### `get_source_health`

```python
get_source_health(module_id: str | None = None)
```

### `get_student_handbook`

```python
get_student_handbook(keyword: str | None = None, cursor: str | None = None, limit: int = 20)
```

### `get_syllabus`

```python
get_syllabus(xnm: str, xqm: str, cursor: str | None = None, limit: int = 50)
```

### `get_teacher_page`

```python
get_teacher_page(keyword: str, detail: bool = False, cursor: str | None = None, limit: int = 50)
```

### `get_teaching_plan_courses`

```python
get_teaching_plan_courses(plan_ref: str, keyword: str | None = None, cursor: str | None = None, limit: int = 50)
```

### `get_teaching_plans`

```python
get_teaching_plans(year: str, keyword: str | None = None, cursor: str | None = None, limit: int = 50)
```

### `get_timetable`

```python
get_timetable(start_date: str, end_date: str, cursor: str | None=None, limit: int=50, force_refresh: bool=False)
```

### `get_timetable_web`

```python
get_timetable_web(xnm: str, xqm: str, cursor: str | None=None, limit: int=50, force_refresh: bool=False)
```

### `get_training_plan_doc`

```python
get_training_plan_doc(plan_name: str, grade: str)
```

### `get_training_plans`

```python
get_training_plans(year: str, keyword: str | None = None, cursor: str | None = None, limit: int = 50)
```

### `list_campus_capabilities`

```python
list_campus_capabilities()
```

### `list_campus_services`

```python
list_campus_services(keyword: str | None = None, cursor: str | None = None, limit: int = 50)
```


## 新增工具的使用边界

### `get_mp_articles`

```python
get_mp_articles(mp_name: str | None = None, keyword: str | None = None, days: int = 7, limit: int = 20, offset: int = 0)
```

查询本机 we-mp-rss 订阅库中的文章标题、摘要和来源链接。`mp_name` 与 `keyword` 用于筛选；`days=0` 不限制日期；`limit` 控制每页数量，`offset` 用于继续读取。数据范围取决于本地订阅和采集结果，并非全校公众号；未找到不等于学校没有相关信息。跨页期间若上游数据变化，可能出现重复或遗漏。

返回的记录时间可能是采集时间而不是实际发布时间，最新一次成功采集时间未知。查询时间只代表本次读取时间；无法以此证明信息最新或采集完整。结果中的链接经过安全处理；不安全链接不会作为可直接访问的链接返回，勿手动恢复被过滤的敏感参数。

服务只读本地 SQLite，独立的上游采集器及其网络和其他行为不受此只读保证约束。配置实际数据库位置时，可为 MCP 进程设置以下环境变量（示例为通用占位路径）：

```powershell
$env:ZJGSU_MP_RSS_DB = "C:/path/to/local/subscriptions.db"
```

库不存在、不可读或不兼容时按不可用处理，不应解释成没有文章。有关验证范围见[验证记录](validation.md)。

- `get_library_seats`：尚未验证可用的 HTTPS 业务入口，服务默认**拒绝通过 HTTP 发送会话 Cookie** 并提供官方人工入口。不为日常查询启用明文排障开关。
- `get_dorm_electric`：`summary` 读取余额/状态，`bill` 读取最近月账单，`detail` 接受 `YYYY-MM`（省略时固定本次有效月份）。园区/楼栋/楼层/房间解析遇歧义时拒绝；旧配置首次绑定会比较双方已有的非空定位字段，冲突时拒绝取源并要求重新解析。匿名可达与本机绑定都不证明设备归属，仅限本人明确授权设备。
- 余座与电表均带专用 outputSchema 和最小 freshness 契约；失败有同范围旧成功快照时返回 `partial`、`stale` 与 `as_of`，不把旧数据当当前状态。`source=live` 仅表示本次成功读取，设备刷新规律与余座后续变化仍由来源决定。
- `get_training_plan_doc`：只接受唯一精确同名方案，按源 `njdm_id` 多值及回退字段核对年级，畸形或冲突字段拒绝。文件键包含账号、年级和方案名哈希；下载体经 ZIP/DOCX 校验后逐文件原子替换并校验回滚，不构成跨文件/跨进程事务。提取结果保留完整性标志。
- 教务激活记忆默认开启，可用 `ZJGSU_JW_ACTIVATION_MEMO=0` 关闭。同会话同功能码成功激活后可省去 GET；明确 910 或跳过后的可疑空结果会补激活并最多重试一次，激活失败立即停止。同会话跨功能码由会话锁协调；不承诺学校线上或客户端端到端秒数。

本人借阅不提供；校园卡、畅约和跳转畅约的入口只作简介与人工导航。所有工具不提供选课、报名、签到、预约、支付、审批或提交操作。
