# -*- coding: utf-8 -*-
"""「我的商大」只读校园 MCP —— 源码包。

分层（与 docs/技术设计与Agent交接.md 第 10 节的拟议结构对应，Python 命名）：
- domain/            业务数据模型（工具输出包装契约）与错误码
- adapters/campus/   数据源适配层：认证/HTTP/解析/字段校验（可替换、可测试）
- storage/           本机缓存（SQLite）：按账号+查询范围隔离的快照与来源健康
- auth/              本机凭据层：登录初始化 CLI 与会话存取（与 MCP 请求协议分离）
- service/           应用服务：编排「读取→分类→缓存→包装」并产出工具输出
- mcp_server/        MCP 服务器（stdio；只读工具注册）

命名说明：目录名用 mcp_server 而非 mcp，避免与官方 SDK 的 `import mcp` 在源码树中混淆。
"""
