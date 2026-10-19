# -*- coding: utf-8 -*-
"""DES 桥测试：适配器自有的 DesTool.java 复现标准向量（与探针同一下游）。

- 标准向量：key=133457799BBCDFF1、plain=0123456789ABCDEF
  期望首块 85E813540F0AB405（Java Cipher("DES") 含 PKCS5 填充 → 总长 32）
- 无 java 环境时自动跳过（不改变其他测试结果）。
"""
from __future__ import annotations

import unittest

from test.helpers import PROJECT_ROOT  # noqa: F401  (sys.path)

from src.adapters.campus.des import des_encrypt_hex, find_java

STD_KEY = "133457799BBCDFF1"
STD_PLAIN = "0123456789ABCDEF"
STD_EXPECT_BLOCK = "85E813540F0AB405"


class DesBridgeTest(unittest.TestCase):
    @unittest.skipUnless(find_java(), "本机无可用 java，跳过 DES 桥测试")
    def test_standard_vector(self):
        java_bin = find_java()
        out1 = des_encrypt_hex(java_bin, STD_KEY, STD_PLAIN)
        out2 = des_encrypt_hex(java_bin, STD_KEY, STD_PLAIN)
        self.assertTrue(out1.startswith(STD_EXPECT_BLOCK), out1)
        self.assertEqual(len(out1), 32)
        self.assertEqual(out1, out2)  # 可重复


if __name__ == "__main__":
    unittest.main()
