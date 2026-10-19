# -*- coding: utf-8 -*-
"""HTTP 传输层（与解析分离，便于注入假传输做合成验收）。

真实实现与探针一致：urllib POST form-urlencoded，User-Agent 为 okhttp（对齐
App 请求特征）；仅使用已核实端点。返回值约定：
- {'kind': 'response', 'status': <int>, 'text': <str>}
- {'kind': 'network_error', 'err': <str>}   # err 仅用于本机诊断，不写入对外输出
"""
from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request

# 证书校验保持开启；进程内复用同一 SSLContext，避免每请求重新加载 CA（约 20ms/次）
_SSL_CONTEXT = ssl.create_default_context()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_SSL_CONTEXT))

DEFAULT_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    # 与 App 一致的客户端特征；不含任何凭据。
    "User-Agent": "okhttp/3.14.9",
}


class UrllibTransport:
    """POST form 传输（生产实现）。"""

    def post_form(self, url: str, form: dict | None = None,
                  headers: dict | None = None, timeout: int = 20) -> dict:
        data = urllib.parse.urlencode(form or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in DEFAULT_HEADERS.items():
            req.add_header(k, v)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return {"kind": "response", "status": resp.status,
                        "text": resp.read().decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            return {"kind": "response", "status": e.code, "text": body}
        except Exception as e:
            return {"kind": "network_error", "err": str(e)}
