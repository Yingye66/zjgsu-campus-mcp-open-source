# -*- coding: utf-8 -*-
"""DES 加密桥（复现 App 的 Key.eCode 底层算法；用于登录第一步的 mobileBT 参数）。

与探针同构（证据：探针 --offline-check 已实测标准向量 85E813540F0AB405）：
- 算法 = Java Cipher("DES")（DES/ECB/PKCS5Padding），与 App 代码同构；
- 密钥与明文经 stdin 传入 Java 子进程（不出现在进程参数表）；
- 本机 Node 无法创建子进程（实测 EBUSY，见本地检查记录），故桥走 Python→Java
  （本机与用户环境均实测可用）。
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

DES_TOOL = Path(__file__).resolve().parent / "java" / "DesTool.java"


def find_java() -> str | None:
    """按 JAVA_HOME → PATH 查找 java。"""
    cands: list[str] = []
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(os.path.join(jh, "bin", "java.exe"))
    cands.append("java")
    for c in cands:
        try:
            subprocess.run([c, "-version"], capture_output=True, timeout=30)
            return c
        except Exception:
            continue
    return None


def des_encrypt_hex(java_bin: str, key_hex: str, plain_hex: str) -> str:
    """经 stdin 传入密钥/明文，返回大写 hex 密文。"""
    stdin_data = (key_hex + "\n" + plain_hex + "\n").encode("ascii")
    r = subprocess.run([java_bin, str(DES_TOOL)], input=stdin_data,
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        # stderr 不转述（避免回显）；只给通用失败信息
        raise RuntimeError("DesTool 退出码 %s" % r.returncode)
    return r.stdout.decode("utf-8", "replace").strip().upper()


def ecode(java_bin: str, des_key_b64: str, username: str) -> str:
    """复现 Key.eCode：BASE64(DES/ECB/PKCS5(用户名))。"""
    key_hex = base64.b64decode(des_key_b64).hex()
    plain_hex = username.encode("utf-8").hex()
    enc_hex = des_encrypt_hex(java_bin, key_hex, plain_hex)
    return base64.b64encode(bytes.fromhex(enc_hex)).decode("ascii")
