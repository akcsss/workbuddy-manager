"""Account-risk UI regression with mocked APIs only; requires Playwright.

Serve web/out at 127.0.0.1:7960, then run this script.
WB_BASE, WB_SHOTS and WB_CHROMIUM override the server, screenshots and browser.
"""
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

base = os.environ.get('WB_BASE', 'http://127.0.0.1:7960')
shots = Path(os.environ.get('WB_SHOTS', str(Path(tempfile.gettempdir()) / 'wb-risk-ui')))
shots.mkdir(parents=True, exist_ok=True)
now = int(time.time())
common = dict(realm='cn', expires_at=now+86400*30, remain_seconds=86400*30,
              is_expired=False, in_pool=True, disabled=False, success_count=2, credits=100)
accounts = [dict(common, uid='good', nickname='正常账号', file='workbuddy-good.json', healthy=True),
            dict(common, uid='paused', nickname='保护暂停账号', file='workbuddy-paused.json', healthy=False,
                 manual_disabled=True, manual_reason='account-risk: token_refresh HTTP 429')]
state = dict(ack=False, unknown=False, role='admin')

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ.get('WB_CHROMIUM'))
    context = browser.new_context(viewport={'width':1440,'height':1000}, locale='zh-CN')
    context.add_init_script("localStorage.setItem('workbuddy-manager:locale','zh-CN');localStorage.setItem('theme','dark');localStorage.setItem('workbuddy-manager:dock-tip-dismissed','true')")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def api(route):
        path = urlparse(route.request.url).path
        data = {}
        if path == '/api/me': data = dict(username='regression', role=state['role'])
        elif path == '/api/upstreams': data = dict(items=[dict(id=1,name='测试分组',auth_dir='/auths',enabled=True,is_default=True)])
        elif path == '/api/accounts': data = dict(total=2, accounts=accounts, manageable=True)
        elif path == '/api/status': data = dict(connected=True, accounts=accounts, total=2, healthy=1, disabled=1)
        elif path == '/api/accounts/refresh-credits': data = dict(total=2, succeeded=2, failed=[], credits={'good':100,'paused':100}, meta={})
        elif path == '/api/stats/daily': data = []
        elif path == '/api/stats/credits': data = dict(today={},week={},total={},daily=[])
        elif path == '/api/accounts/admin-api-status': data = dict(enabled=True)
        elif path.startswith('/api/account-risk/'):
            assert route.request.method == 'POST'
            state['ack'] = True
            data = dict(ok=True)
        elif path == '/api/account-risk':
            warning = dict(id='paused-event',uid='paused',nickname='保护暂停账号',group='测试分组',realm='cn',
                           operation='token_refresh',http_status=429,ts=now,historical=False,
                           acknowledged=state['ack'],paused=None if state['unknown'] else True,action_error='')
            history = dict(warning,id='history-event',uid='good',nickname='正常账号',ts=now-86400,
                           historical=True,paused=False,acknowledged=False)
            data = dict(items=[warning] + ([] if state['ack'] else [history]),failed_groups=['测试分组'] if state['unknown'] else [])
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
    context.route('**/api/**', api)
    page.goto(base+'/dashboard', wait_until='networkidle')
    panel = page.get_by_role('region', name='账号保护预警')
    panel.wait_for()
    assert '已保护性暂停' in panel.inner_text()
    assert '历史记录（未据此自动暂停）' in panel.inner_text()
    assert '不是封禁' in panel.inner_text()
    for width in (1440, 390):
        page.set_viewport_size({'width':width,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(shots/f'risk-dashboard-{width}.png'))
    page.get_by_role('button',name='确认当前预警',exact=True).click()
    page.get_by_role('button',name='确认当前预警',exact=True).wait_for(state='hidden')
    assert '已保护性暂停' in panel.inner_text(), 'Acknowledging must not resume the account'
    assert '历史记录' not in panel.inner_text()
    page.reload(wait_until='networkidle')
    assert '已保护性暂停' in panel.inner_text(), 'An acknowledged active pause must survive reload'
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(base+'/accounts',wait_until='networkidle')
    page.locator('tbody tr').first.wait_for()
    assert page.get_by_role('region', name='账号保护预警').count() == 1
    good = page.locator('tbody tr').filter(has_text='正常账号')
    assert '在线' in good.inner_text(), 'Healthy account must remain usable'
    page.screenshot(path=str(shots/'risk-accounts.png'))
    state['unknown'] = True
    page.reload(wait_until='networkidle')
    panel = page.get_by_role('region', name='账号保护预警')
    panel.locator('summary').click()
    assert '暂停状态待确认' in panel.inner_text()
    assert '暂时无法确认' in panel.inner_text()
    assert not errors, errors
    browser.close()
    print('PASS: persistent risk warnings on dashboard/accounts, acknowledgement does not resume, historical/active/unknown states, healthy account unaffected, desktop/mobile layout, no browser errors')
