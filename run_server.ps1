# 启动「我的商大」只读 MCP 服务器（stdio）。
#
# 用法（在任意终端窗口）：
#     powershell -ExecutionPolicy Bypass -File .\run_server.ps1
#
# 说明：
# - stdio 传输：本脚本直接运行服务器进程；由 MCP 客户端负责拉起时通常不需要本脚本，
#   手动验收/排查时用它查看启动输出。
# - 需要先用同目录的 .venv 完成依赖安装（见 技术逻辑说明.md「如何启动」）。
$ErrorActionPreference = "Stop"
# 用 $PSScriptRoot 而不是 $MyInvocation.MyCommand.Path：后者在某些调用方式下是空字符串，
# 会让 Join-Path 拿到空路径直接抛错，脚本表现成「什么都没做就退出」。
$ComponentRoot = $PSScriptRoot
if (-not $ComponentRoot) {
    throw "无法确定脚本所在目录（PSScriptRoot 为空）。请用完整路径调用本脚本。"
}
$Python = Join-Path $ComponentRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "项目虚拟环境不存在：$Python（请先按 技术逻辑说明.md 创建 .venv 并安装依赖）"
}

Set-Location $ComponentRoot
& $Python -m src.mcp_server.server
exit $LASTEXITCODE
