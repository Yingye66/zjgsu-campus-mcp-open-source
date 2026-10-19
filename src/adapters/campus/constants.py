# -*- coding: utf-8 -*-
"""客户端常量（clientId / DES 密钥）的提取与本地私有存储。

这些是 APK 内的客户端常量（非用户凭据），登录第一步需要：
- clientId：来自 LoginActivity 的 setAppKey("...")；
- DES 密钥：来自 Key 类 encryptKey（用于 mobileBT = BASE64(DES(用户名))）。

存储策略（设计选择与依据）：
- 优先从本地私有文件 data/client_constants.json 读取（该文件被 .gitignore 排除）；
- 文件不存在时，从反编译产物动态提取一次并写入——之后不再依赖 validation/ 目录
  （validation 属可删实验产物，见交接文档证据清理边界）；
- 密钥不写入文档、报告、日志或工具输出；文件位于项目 data/ 私有目录。
- 已知限制：文件为明文（本机用户目录 + .gitignore 保护；未做 Windows ACL 收紧），
  详见技术逻辑说明「已知的坑和限制」。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_JADX_SOURCES = None
CONSTANTS_FILE = "client_constants.json"


class CampusConfigError(Exception):
    """客户端常量缺失且无法提取。"""


def _extract(pattern: str, path: Path, label: str) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(pattern, text)
    if not m:
        raise CampusConfigError("无法从 %s 提取 %s" % (path, label))
    return m.group(1)


def extract_from_jadx(jadx_sources: Path) -> dict:
    """从反编译产物提取客户端常量（值不回显）。"""
    client_id = _extract(
        r'setAppKey\("([^"]+)"\)',
        jadx_sources / "com" / "zfsoft" / "newzjgs" / "mvp" / "ui" / "activity" / "LoginActivity.java",
        "clientId(appKey)")
    des_key_b64 = _extract(
        r'encryptKey\s*=\s*"([^"]+)"',
        jadx_sources / "com" / "zfsoft" / "newzjgs" / "utils" / "base64" / "Key.java",
        "DES key")
    return {"schema": 1, "client_id": client_id, "des_key_b64": des_key_b64,
            "source": "extracted_from_jadx"}


def load_client_constants(data_dir: Path, jadx_sources: Path | None = None) -> dict:
    """读取本地私有常量文件；不存在则提取并落盘（私有目录）。"""
    path = Path(data_dir) / CONSTANTS_FILE
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("client_id") and data.get("des_key_b64"):
            return data
    src = Path(jadx_sources) if jadx_sources else DEFAULT_JADX_SOURCES
    if src is None or not src.exists():
        raise CampusConfigError(
            "客户端常量缺失：%s 不存在，且反编译产物（%s）不可用。"
            "请在私有数据目录配置 client_constants.json，步骤见 docs/quickstart.md。" % (path, src))
    data = extract_from_jadx(src)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
