import json
import os
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch
from src.service.mp_weread_content_service import MpWereadContentService, read_weread_auth, _RateLimiter, _fetch, _NoRedirect, _UPSTREAM_PYTHON
from test.helpers import temp_dir

FEED='MP_WXS_12345'
ARTICLE='12345-12345_token-123'
SECRET='secret-placeholder-never-report'
PAGE='''<html><h1 class="rich_media_title">校园生活</h1><a id="js_name">示例食堂</a><div id="js_content"><p>菌菇面 12 元</p></div></html>'''.encode()

class PassLimiter:
    def run(self, fetch, review, cookie):
        return True, fetch(review, cookie)

class WereadContentTests(unittest.TestCase):
    def service(self, fetch=lambda review,cookie: (200,PAGE,'utf-8'), auth=lambda: SECRET):
        return MpWereadContentService(auth_loader=auth, fetch=fetch, limiter=PassLimiter())

    def test_body_only_answer_and_identity_match(self):
        result=self.service().read(ARTICLE,FEED,expected_title='校园生活',expected_source='示例食堂')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['data']['text'],'菌菇面 12 元')
        self.assertEqual(result['identity_status'],'metadata_consistent')
        self.assertEqual(result['source'],'weread')
        self.assertNotIn(SECRET,repr(result)); self.assertNotIn(ARTICLE,repr(result))

    def test_bad_id_rejected_before_credentials_or_network(self):
        def forbidden(): raise AssertionError('should not load')
        for aid,feed in [('bad',FEED),(ARTICLE,'MP_WXS_999'),('12345-12345_'+('MP_WXS_')+'z',FEED),('12345-12345_a%20',FEED)]:
            with self.subTest(aid=aid,feed=feed):
                result=self.service(auth=forbidden).read(aid,feed)
                self.assertEqual(result['status'],'unavailable')
                self.assertEqual(result['code'],'invalid_article_identity')

    def test_missing_credentials_is_not_reported_as_empty_article(self):
        result=self.service(auth=lambda: None).read(ARTICLE,FEED)
        self.assertEqual(result['status'],'unavailable'); self.assertEqual(result['code'],'auth_required')

    def test_json_auth_error_is_not_article_body(self):
        result=self.service(fetch=lambda r,c:(200,b'{"errCode":-2012,"msg":"secret-placeholder-never-report"}','utf-8')).read(ARTICLE,FEED)
        self.assertEqual(result['status'],'unavailable'); self.assertEqual(result['code'],'auth_or_quota')
        self.assertNotIn(SECRET,repr(result))

    def test_redirect_refused_and_no_response_leak(self):
        result=self.service(fetch=lambda r,c:(302,b'secret-placeholder-never-report','utf-8')).read(ARTICLE,FEED)
        self.assertEqual(result['status'],'unavailable'); self.assertEqual(result['code'],'redirect_refused')
        self.assertNotIn(SECRET,repr(result))

    def test_source_rate_limit_stops_batch_retry(self):
        result=self.service(fetch=lambda r,c:(429,b'',None)).read(ARTICLE,FEED)
        self.assertEqual(result['code'],'rate_limited')

    def test_same_process_request_limiter_waits_before_next_request(self):
        clock=[10.0]
        sleeps=[]
        def sleep(delay):
            sleeps.append(delay)
            clock[0]+=delay
        limiter=_RateLimiter(clock=lambda:clock[0],sleep=sleep)
        first=limiter.run(lambda *_:'a','r','c')
        second=limiter.run(lambda *_:'b','r','c')
        self.assertEqual(first,(True,'a')); self.assertEqual(second,(True,'b'))
        self.assertEqual(sleeps,[2.0])

    def test_fetch_fixed_host_with_no_redirect_handler(self):
        with patch('src.service.mp_weread_content_service.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=urllib.error.HTTPError(
                'https://weread.qq.com/web/mp/content',302,'redirect',{},None)
            result=_fetch('MP_WXS_12345_token-123',SECRET)
            self.assertEqual(result,(302,None,None))
            self.assertIsInstance(build.call_args.args[0],_NoRedirect)
            request=build.return_value.open.call_args.args[0]
            self.assertEqual(request.host,'weread.qq.com')
            self.assertEqual(request.type,'https')
            self.assertEqual(build.return_value.open.call_count,1)

    def test_yaml_credentials_read_only_from_synthetic_upstream_file(self):
        with temp_dir() as directory:
            path=Path(directory)/'wx.lic'
            sample='weread_data:\n  accounts:\n  - cookie: "wr_vid=synthetic; wr_skey=sample"\n'
            path.write_text(sample,encoding='utf8')
            python = Path(os.environ.get('WEREAD_TEST_PYTHON', str(_UPSTREAM_PYTHON)))
            if not python.is_file():
                self.skipTest('Optional we-mp-rss Python with PyYAML is unavailable')
            with patch('src.service.mp_weread_content_service._CREDENTIAL_FILE',path), \
                 patch('src.service.mp_weread_content_service._UPSTREAM_PYTHON',python):
                self.assertEqual(read_weread_auth(),'wr_vid=synthetic; wr_skey=sample')
            self.assertEqual(path.read_text(encoding='utf8'),sample)

    def test_large_page_is_refused(self):
        result=self.service(fetch=lambda r,c:(200,b'x'*(6*1024*1024+1),'utf-8')).read(ARTICLE,FEED)
        self.assertEqual(result['status'],'unavailable'); self.assertEqual(result['code'],'response_invalid')

    def test_network_error_does_not_echo_credentials_or_identity(self):
        def fail(review,cookie): raise OSError(SECRET+review)
        result=self.service(fetch=fail).read(ARTICLE,FEED)
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn(SECRET,repr(result)); self.assertNotIn(ARTICLE,repr(result))

    def test_wrong_title_or_publisher_does_not_attribute_body(self):
        for title,source in [('其他','示例食堂'),('校园生活','其他')]:
            result=self.service().read(ARTICLE,FEED,expected_title=title,expected_source=source)
            self.assertEqual(result['status'],'unavailable'); self.assertEqual(result['code'],'identity_mismatch')
            self.assertNotIn('text',result.get('data',{}))

    def test_missing_metadata_is_explicitly_unverified(self):
        body=b'<div id="js_content">hello</div>'
        result=self.service(fetch=lambda r,c:(200,body,'utf-8')).read(ARTICLE,FEED,expected_title='x',expected_source='y')
        self.assertEqual(result['status'],'ok'); self.assertEqual(result['identity_status'],'unverified')
        self.assertFalse(result['coverage']['complete'])

    def test_intercept_or_broken_html_cannot_be_success(self):
        for payload in ['<div id="js_content">该内容已被发布者删除</div>','<div id="js_content">无结束']:
            result=self.service(fetch=lambda r,c:(200,payload.encode(), 'utf-8')).read(ARTICLE,FEED)
            self.assertEqual(result['status'],'unavailable')

    def test_credentials_loader_rejects_multiple_accounts(self):
        with patch('src.service.mp_weread_content_service.subprocess.run') as run:
            run.return_value.returncode=0
            run.return_value.stdout=b'{"account_count":2}'
            self.assertIsNone(read_weread_auth())

if __name__=='__main__': unittest.main()
