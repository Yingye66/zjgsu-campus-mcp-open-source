# 从零连接与日常使用

## 先理解三个组件

1. **AI 客户端**：你输入自然语言的地方，需要支持本地 stdio MCP。
2. **本项目**：由客户端启动的 Python 查询进程。
3. **学校系统**：数据的原始来源，仍检查账号、权限和网络条件。

优先从公开问题开始。只看到「连接成功」不代表个人查询已经认证；工具列表存在也不代表每个来源都能实时读取。

## 安装与连接

在仓库根目录创建 Python 3.12+ 虚拟环境，用 requirements.txt 安装依赖。
将 examples/mcp-config.example.json 的三个路径替换为本机绝对路径。
command 是虚拟环境 Python；PYTHONPATH 是仓库根；ZJGSU_CAMPUS_DATA_DIR 是你的私人数据目录。
不要向 MCP 配置填学校密码、token 或 Cookie。

保存配置后，在客户端重载一次该 MCP，并询问「查最近的奖学金公示，说明来源与读取范围」。
连接更新时只重载本项目，不需要重装其他服务。Windows 可使用 run_server.ps1 手动排查启动；通常由客户端启动即可。

成绩、成绩明细、两种课表及考试会复用60秒内的成功非空结果。需要最新时说“强制刷新，不使用缓存”，客户端应传`force_refresh=true`；返回`freshness.cache_hit`标明是否联网取数。首次访问仍取决于学校响应，详见[查询提速](performance.md)。

## 已有用户更新

默认main现为47工具版本。先在仓库根目录用`git status`确认并妥善保存本地修改，再执行`git pull --ff-only`；用本项目虚拟环境执行`python -m pip install -r requirements.txt`同步已声明依赖，包含培养方案正文使用的python-docx。确认MCP配置指向这份检出目录，重载本项目后客户端应发现47工具。GitHub更新不会自动替换另一个运行目录，也不应仅因升级重新登录或删除私人data。

图书馆余座默认不发送明文身份Cookie，问到时提供官方人工入口；不要为日常使用开启不安全排障开关。电表需要本人核实的定位配置，匿名接口可达不证明设备归属；已有非空定位冲突会拒绝迁移。培养方案附件需有效教务会话及精确方案/年级，并检查文件身份、版本和文本完整性。本人借阅取消、校园卡与畅约仅人工导航，均不因升级恢复自动查询。

## 本人会话

个人查询需本人合法账号、能访问相应学校来源的网络，以及私有客户端配置。
App 登录的 DES Java 源码桥需要 JDK，设置 JAVA_HOME 或把 java 放入 PATH；可先运行 java -version 检查。

仓库只提供 examples/client-constants.example.json 的字段模板，不提供真实值。
如你已经合法持有该本地配置，将实际 client_constants.json 放到私人数据目录；不要把占位模板当真实配置，也不要提交该文件。
当前发布副本移除了开发机器的绝对路径回退，不再假设其他机器拥有开发者的反编译目录。
没有真实私有配置时，个人初始化不能完整复现；公开查询仍可先使用。

在本人交互式 PowerShell、仓库根目录执行，数据目录必须与 MCP 一致：

```powershell
$env:ZJGSU_CAMPUS_DATA_DIR = "C:/path/private-campus-data"
& .\.venv\Scripts\python.exe -m src.auth.login_cli init
& .\.venv\Scripts\python.exe -m src.auth.login_cli status
```

密码只输入本人终端，不回显。init 可能保存 App 会话但未成功建立教务会话；按具体成功提示和查询结果判断。
不要因为来源改版、权限拒绝或代码结构错误反复登录。

### 可选：保存凭据并自动恢复

如果希望会话过期后继续直接提问，在本人终端显式开启。数据目录须与MCP配置一致，密码由终端隐藏输入，不发送给AI：

```powershell
& .\.venv\Scripts\python.exe -m src.auth.login_cli cred-save
& .\.venv\Scripts\python.exe -m src.auth.login_cli cred-status
```

首次初始化也可用 `init --save-cred` 同时登录并保存；普通 `init` 不保存密码。默认时效180天，从保存时间开始；超期表示拒绝继续自动使用，不表示已经证实密码变更。旧版本无版本号的凭据文件需重新执行cred-save。

开启后，工具识别统一会话失效时自动重登、重试一次，在issues中标记auto_relogin；成功、partial和失败分别说明，不把重登本身当作查询成功。缺配置、权限拒绝或网络故障应按提示处理；认证被拒后同版本凭据停止自动重试，确认后用cred-save更新。体测专用令牌、选座Cookie不由统一登录生成。App课表端点99码仅作为受限恢复线索，不是已证实的学校官方认证错误语义。

需要手动刷新或停用保存的密码时：

```powershell
& .\.venv\Scripts\python.exe -m src.auth.login_cli autologin
& .\.venv\Scripts\python.exe -m src.auth.login_cli cred-clear
& .\.venv\Scripts\python.exe -m src.auth.login_cli logout
```

`cred-clear`删除保存的密码，不删除会话或缓存；`logout`清除活动会话。如果保留凭据又继续查询，可能再次自动登录，所以停止自动恢复时先cred-clear，再按需logout或在客户端禁用本MCP。清除不是安全擦除备份；自行保护私人目录及备份。

密码保存在私人数据目录的credentials.json中，为**明文JSON**。`.gitignore`不是加密或访问控制，Windows ACL未自动收紧。不要把私人目录上传、分享或放进公开同步目录。本次未读取或迁移你的真实凭据，更新代码后只需重载MCP；是否保存密码由使用者自行选择。

常驻MCP和终端命令以数据目录中的`.auth.lock`协调本地认证文件提交。锁只覆盖本地校验/写入，学校登录等待不持锁；不要在进程运行时删除锁文件。它不使所有学校业务查询都具备跨进程事务，其他来源的限制仍适用。

体测使用独立专用会话。现有临时导入命令为 fitness-token，只在确实拥有合法且有效的本人专用令牌时，在终端交互执行；init 不会自动生成该令牌。
本次验证没有证明所有账号的专用令牌来源和归属。不能把令牌发给 AI，不能拿其他人的令牌。

## 直接提问

- 课表/考试/成绩：注明日期或学期；客户端负责转换成工具参数。
- 通知/公示：先列表，再点名读一条正文；正文、附件链接与附件内容是不同层次。
- 学生手册：问具体事项，并要求原文出处与版本。
- 不可用结果：说明缺什么，不将其变成「没有」或编造学校数据。

## 故障对照

| 表现 | 下一步 |
| --- | --- |
| 没发现工具 | 检查 command、PYTHONPATH 和 Python 版本，重载一次 |
| auth_required | 检查是否是需要登录的来源，并恢复对应会话 |
| 上游拒绝/网络错误 | 保留工具名、范围和时间；核学校网络/来源状态，不反复输入密码 |
| partial | 看数据时间与缺口，可能返回旧成功快照 |
| 结构错误或工具异常 | 按对应分支维护，不修改原始学校数据 |
| 私有常量缺失 | 个人登录前提尚未满足；公开功能可继续，勿使用占位值登录 |

## 停用与恢复

在客户端禁用或移除本 MCP 条目即可停止启动；无需删除私人数据。
logout 会清除当前活动会话，缓存行可能仍在本机 SQLite 中；它不代表安全擦除所有历史数据。
重新启用时恢复原配置路径，需要认证的来源再按需恢复会话。
