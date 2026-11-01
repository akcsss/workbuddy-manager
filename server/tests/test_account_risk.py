import asyncio
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import time
import unittest
from unittest import mock

from server import config, db, upstreamsvc
from server.services import accountrisk, tencent, wb2api


class AccountRiskTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the frontend status test')
    def test_frontend_safety_pause_status(self):
        node = shutil.which('node')
        version = subprocess.check_output([node, '--version'], text=True).strip().lstrip('v')
        if tuple(map(int, version.split('.')[:2])) < (22, 6):
            self.skipTest('Node >= 22.6 is required')
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([node, '--experimental-strip-types', str(root/'web/lib/account-risk-status.test.mjs')],
                                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = config.DB_PATH, config.AUTH_DIR, config.USERS_FILE
        config.DB_PATH = self.root / 'manager.db'
        config.AUTH_DIR = self.root / 'auths'
        config.AUTH_DIR.mkdir()
        config.USERS_FILE = self.root / 'users.json'
        db._conn = None
        db.connect()
        self.now = int(time.time())
        self.uid = 'abcd1234-0000-0000-0000-000000000001'
        self.filename = 'workbuddy-test.json'
        (config.AUTH_DIR / self.filename).write_text(json.dumps({
            'account': {'uid': self.uid, 'nickname': 'risk-test'},
            'auth': {'accessToken': 'at', 'refreshToken': 'rt', 'expiresAt': self.now + 86400,
                     'realm': 'cn', 'domain': 'copilot.tencent.com'},
        }), encoding='utf-8')
        self.pool = {'connected': True, 'accounts': [{'uid': self.uid, 'realm': 'cn', 'disabled': False}]}
        self.status_patch = mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(side_effect=lambda **kw: self.pool))
        self.status_patch.start()
        accountrisk.initialize()
        db.set_setting('account_risk_enabled_at', self.now)

    def tearDown(self):
        self.status_patch.stop()
        if db._conn:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.AUTH_DIR, config.USERS_FILE = self.original
        self.temp.cleanup()

    def event(self, **kwargs):
        return dict({'ts': self.now, 'uid': self.uid[:8], 'kind': 'keepalive',
                     'message': 'upstream soft_rate (http 429): Too Many Requests'}, **kwargs)

    def test_auth_429_pauses_one_account_without_restart(self):
        async def pause(uid, disabled, reason, **kwargs):
            self.assertEqual(uid, self.uid)
            self.pool['accounts'][0].update(manual_disabled=disabled, manual_reason=reason)
            return True, '', 'ok'
        with mock.patch.object(wb2api, 'set_manual_disabled', new=mock.AsyncMock(side_effect=pause)) as action, \
             mock.patch.object(wb2api, 'restart_container') as restart:
            asyncio.run(accountrisk.handle_tasks([self.event()]))
            asyncio.run(accountrisk.handle_tasks([self.event()]))
            action.assert_awaited_once()
            restart.assert_not_called()
        items = asyncio.run(accountrisk.warnings())['items']
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['paused'])
        self.assertNotIn('scope', items[0])

    def test_historical_failures_warn_without_pausing(self):
        with mock.patch.object(wb2api, 'set_manual_disabled', new=mock.AsyncMock()) as action:
            asyncio.run(accountrisk.handle_tasks([self.event(ts=self.now-86400)]))
            action.assert_not_awaited()
        item = asyncio.run(accountrisk.warnings())['items'][0]
        self.assertTrue(item['historical'])
        self.assertFalse(item['paused'])

    def test_new_live_failure_after_enable_is_not_hidden_by_same_second_dedup(self):
        async def pause(uid, disabled, reason, **kwargs):
            self.pool['accounts'][0].update(manual_disabled=disabled, manual_reason=reason)
            return True, '', 'ok'
        with mock.patch.object(accountrisk.time, 'time', return_value=self.now), \
             mock.patch.object(wb2api, 'set_manual_disabled', new=mock.AsyncMock(side_effect=pause)) as action:
            self.assertTrue(asyncio.run(accountrisk.pause_refresh(self.uid, config.AUTH_DIR)))
            item = asyncio.run(accountrisk.warnings())['items'][0]
            accountrisk.acknowledge(item['id'])
            self.pool['accounts'][0].update(manual_disabled=False, manual_reason='')
            self.assertTrue(asyncio.run(accountrisk.pause_refresh(self.uid, config.AUTH_DIR)))
            self.assertEqual(action.await_count, 2)
            self.assertFalse(asyncio.run(accountrisk.warnings())['items'][0]['acknowledged'])

    def test_model_quota_not_classified_and_server_error_only_warns(self):
        self.assertIsNone(accountrisk.classify_task({'kind': 'chat', 'message': '6004 model quota exhausted'}))
        self.assertIsNone(accountrisk.classify_task({'kind': 'chat', 'message': 'upstream soft_rate (http 429)'}))
        with mock.patch.object(wb2api, 'set_manual_disabled', new=mock.AsyncMock()) as action:
            asyncio.run(accountrisk.handle_tasks([self.event(kind='travel', message='claim: http 500 grant credits failed')]))
            action.assert_not_awaited()
        self.assertEqual(asyncio.run(accountrisk.warnings())['items'][0]['http_status'], 500)

    def test_ambiguous_uid_prefix_never_pauses(self):
        account = wb2api.list_auth_accounts()[0]
        with mock.patch.object(wb2api, 'list_auth_accounts', return_value=[account, {**account, 'uid':self.uid[:8]+'-other'}]), \
             mock.patch.object(wb2api, 'set_manual_disabled', new=mock.AsyncMock()) as action:
            asyncio.run(accountrisk.handle_tasks([self.event()]))
            action.assert_not_awaited()

    def test_no_admin_route_uses_file_pause_without_restart(self):
        with mock.patch.object(wb2api, 'set_manual_disabled', new=mock.AsyncMock(return_value=(False, '', 'no_route'))):
            self.assertTrue(asyncio.run(accountrisk.pause_refresh(self.uid, config.AUTH_DIR)))
        self.assertTrue((config.AUTH_DIR / (self.filename+'.disabled')).is_file())
        self.assertTrue(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))

    def test_keepalive_summary_retains_actual_attempt_denominator(self):
        from server.services import tasklog
        event = tasklog.parse_line('2026-11-01T01:00:00Z keepalive done: checked=49 ok=35 failed=14 skipped=0')
        self.assertEqual(event['kind'], 'keepalive')
        self.assertEqual(event['uid'], '')
        self.assertIn('尝试 49，成功 35，失败 14', event['message'])

    def test_pause_failure_is_visible(self):
        with mock.patch.object(wb2api, 'set_manual_disabled', new=mock.AsyncMock(return_value=(False, '', 'error'))):
            self.assertFalse(asyncio.run(accountrisk.pause_refresh(self.uid, config.AUTH_DIR)))
        self.assertEqual(asyncio.run(accountrisk.warnings())['items'][0]['action_error'], 'error')

    def test_acknowledging_never_resumes_and_active_warning_remains(self):
        self.pool['accounts'][0].update(manual_disabled=True, manual_reason=accountrisk.PREFIX+'token_refresh HTTP 429')
        item = asyncio.run(accountrisk.warnings())['items'][0]
        self.assertTrue(accountrisk.acknowledge(item['id']))
        self.assertTrue(asyncio.run(accountrisk.warnings())['items'][0]['paused'])
        self.pool['accounts'][0].update(manual_disabled=False, manual_reason='')
        self.assertEqual(asyncio.run(accountrisk.warnings())['items'], [])
        self.assertFalse(accountrisk.acknowledge('not-an-event'))

    def test_warnings_survive_database_reopen_and_unknown_state(self):
        account = wb2api.list_auth_accounts()[0]
        ident, inserted = accountrisk.record(config.AUTH_DIR, account, 'token_refresh', 429, self.now)
        self.assertTrue(inserted)
        self.assertFalse(accountrisk.record(config.AUTH_DIR, account, 'token_refresh', 429, self.now)[1])
        db._conn.close(); db._conn = None
        self.pool = {'connected': False}
        result = asyncio.run(accountrisk.warnings())
        self.assertIsNone(result['items'][0]['paused'])
        self.assertTrue(result['failed_groups'])

    def test_warning_scope_does_not_leak_to_another_group(self):
        account = wb2api.list_auth_accounts()[0]
        accountrisk.record(config.AUTH_DIR, account, 'token_refresh', 429, self.now)
        other = {**upstreamsvc.default_upstream(), 'id':9, 'name':'other', 'auth_dir':str(self.root/'other')}
        Path(other['auth_dir']).mkdir()
        with mock.patch.object(upstreamsvc, 'get_upstream', return_value=other):
            self.assertEqual(asyncio.run(accountrisk.warnings(group_id=9))['items'], [])

    def test_confirmed_pause_survives_upstream_outage_until_confirmed_enable(self):
        self.pool['accounts'][0].update(manual_disabled=True, manual_reason=accountrisk.PREFIX+'token_refresh HTTP 429')
        self.assertTrue(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))
        self.pool = {'connected':False}
        self.assertTrue(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))
        self.assertFalse(asyncio.run(accountrisk.paused('other', config.AUTH_DIR)))
        self.pool = {'connected':True, 'accounts':[{'uid':self.uid, 'manual_disabled':False}]}
        self.assertFalse(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))

    def test_refresh_guard_prevents_network_when_paused(self):
        payload = {'uid':self.uid, 'refresh_token':'rt', '_risk_auth_dir':str(config.AUTH_DIR)}
        with mock.patch.object(accountrisk, 'paused', new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(tencent, '_account_http_client') as network:
            ok, message, _ = asyncio.run(tencent.refresh_token(payload))
            self.assertFalse(ok)
            self.assertIn('未发送', message)
            network.assert_not_called()

    def test_account_routes_block_paused_account_without_scope_errors(self):
        from fastapi.testclient import TestClient
        from server.main import app
        from server import security
        app.dependency_overrides[security.current_user] = lambda: {'username':'admin', 'role':'admin'}
        client = TestClient(app)
        try:
            with mock.patch.object(accountrisk, 'paused', new=mock.AsyncMock(return_value=True)):
                for action in ('checkin', 'credits', 'test'):
                    request = client.get if action == 'credits' else client.post
                    result = request(f'/api/accounts/{self.filename}/{action}?force=true')
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertIn('保护性暂停', result.text)
        finally:
            app.dependency_overrides.pop(security.current_user, None)
            client.close()

    def test_warning_api_requires_login_and_ack_requires_admin(self):
        from fastapi.testclient import TestClient
        from server.main import app
        from server import security
        client = TestClient(app, raise_server_exceptions=True)
        try:
            with mock.patch.object(security, 'load_users', return_value={'secret':'test-only', 'users':[]}):
                self.assertEqual(client.get('/api/account-risk').status_code, 401)
            app.dependency_overrides[security.current_user] = lambda: {'username':'viewer', 'role':'viewer'}
            try:
                self.assertEqual(client.get('/api/account-risk').status_code, 200)
                self.assertEqual(client.post('/api/account-risk/not-an-event/acknowledge').status_code, 403)
            finally:
                app.dependency_overrides.pop(security.current_user, None)
        finally:
            client.close()
