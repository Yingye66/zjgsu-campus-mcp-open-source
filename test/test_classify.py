# -*- coding: utf-8 -*-
"""分类器回归：与探针 v5 的 31 项合成样本一一对应（确保提取过程未改变语义）。

对照证据：validation/probe/out/report-2026-10-13T13-10-12-863.txt（31/31 PASS）。
"""
from __future__ import annotations

import unittest

from test.helpers import PROJECT_ROOT  # noqa: F401  (确保 sys.path 就绪)

from src.adapters.campus.classify import (COURSE_SUCCESS_CODES, LOGIN_SUCCESS_CODE,
                                          classify_course, classify_login,
                                          classify_usertype, decide_after_login,
                                          decide_after_usertype)


class ClassifyRegressionTest(unittest.TestCase):
    def test_constants_unchanged(self):
        # R09：登录端点成功码 = 字符串 "200"；R11：课表端点白名单 = ("0",)
        self.assertEqual(LOGIN_SUCCESS_CODE, "200")
        self.assertEqual(COURSE_SUCCESS_CODES, ("0",))

    def test_login_cases(self):
        cases = [
            ('登录·成功（字符串 "200" + token）',
             classify_login('response', 200, '{"data":{"code":"200","access_token":"TOK"}}')[0], 'ok'),
            ('登录·成功（数值 200 规范化为 "200"）',
             classify_login('response', 200, '{"data":{"code":200,"access_token":"TOK"}}')[0], 'ok'),
            ('登录·200 但 token 为空串（不放行）',
             classify_login('response', 200, '{"data":{"code":"200","access_token":""}}')[0], 'service_error'),
            ('登录·200 但缺 token 字段（不放行）',
             classify_login('response', 200, '{"data":{"code":"200"}}')[0], 'service_error'),
            ('登录·code=0 且含 token（不接受 0）',
             classify_login('response', 200, '{"data":{"code":"0","access_token":"TOK"}}')[0], 'auth_failed'),
            ('登录·失败码含 token（不采用）',
             classify_login('response', 200, '{"data":{"code":"1","access_token":"TOK"}}')[0], 'auth_failed'),
            ('登录·失败但含 token（流程不继续）',
             decide_after_login(classify_login('response', 200,
                                               '{"data":{"code":"1","access_token":"TOK"}}')[0]), False),
            ('登录·需改密（40010）',
             classify_login('response', 200, '{"data":{"code":"40010","modifyPwdUrl":"u"}}')[0], 'auth_required'),
            ('登录·HTTP 500 但 body 含 200（HTTP 前置判据）',
             classify_login('response', 500, '{"data":{"code":"200","access_token":"TOK"}}')[0], 'http_error'),
            ('登录·超时', classify_login('network_error', 0, '')[0], 'network_error'),
            ('登录·非 JSON', classify_login('response', 200, '<html>login</html>')[0], 'parser_mismatch'),
            ('登录·data 缺失', classify_login('response', 200, '{"message":"m"}')[0], 'service_error'),
            ('登录·code 缺失（不放行）',
             classify_login('response', 200, '{"data":{"access_token":"TOK"}}')[0], 'missing_code'),
            ('登录·data 非法结构（数组）',
             classify_login('response', 200, '{"data":[]}')[0], 'service_error'),
        ]
        for name, got, expect in cases:
            self.assertEqual(got, expect, name)

    def test_usertype_cases(self):
        self.assertTrue(decide_after_usertype(
            *classify_usertype('response', 200, '{"data":{"code":"0","userType":"2"}}')[:2]))
        self.assertFalse(decide_after_usertype(
            *classify_usertype('response', 200, '{"data":{"code":"0","userType":"1"}}')[:2]))
        self.assertFalse(decide_after_usertype(
            *classify_usertype('response', 200, '{"data":{"code":"9"}}')[:2]))

    def test_course_cases(self):
        cases = [
            ('课表·成功非空（已验证成功码 0）',
             classify_course('response', 200, '{"data":{"code":"0","list":[{"kcmc":"K"}]}}')[0], 'ok'),
            ('课表·成功空数组（已验证成功码 0）',
             classify_course('response', 200, '{"data":{"code":"0","list":[]}}')[0], 'ok_empty'),
            ('课表·整数 0 规范化（成功码）',
             classify_course('response', 200, '{"data":{"code":0,"list":[{"kcmc":"K"}]}}')[0], 'ok'),
            ('课表·未知码+非空数组（拒绝，不采用为成功）',
             classify_course('response', 200, '{"data":{"code":"500","list":[{"kcmc":"K"}]}}')[0], 'unknown_code'),
            ('课表·未知码+空数组（拒绝，不建立空快照）',
             classify_course('response', 200, '{"data":{"code":"500","list":[]}}')[0], 'unknown_code'),
            ('课表·98', classify_course('response', 200, '{"data":{"code":"98"}}')[0], 'business_error'),
            ('课表·99', classify_course('response', 200, '{"data":{"code":"99"}}')[0], 'business_error'),
            ('课表·code 缺失', classify_course('response', 200, '{"data":{"list":[]}}')[0], 'missing_code'),
            ('课表·list 非数组',
             classify_course('response', 200, '{"data":{"code":"0","list":{}}}')[0], 'parser_mismatch'),
            ('课表·元素非对象',
             classify_course('response', 200, '{"data":{"code":"0","list":["x"]}}')[0], 'parser_mismatch'),
            ('课表·顶层非对象', classify_course('response', 200, '[1,2]')[0], 'parser_mismatch'),
            ('课表·data 缺失', classify_course('response', 200, '{"message":"m"}')[0], 'service_error'),
            ('课表·HTTP 502', classify_course('response', 502, '')[0], 'http_error'),
            ('课表·超时', classify_course('network_error', 0, '')[0], 'network_error'),
        ]
        for name, got, expect in cases:
            self.assertEqual(got, expect, name)


if __name__ == "__main__":
    unittest.main()
