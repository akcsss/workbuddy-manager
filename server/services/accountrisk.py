"""Account-scoped safety pauses and durable warnings; never stops an upstream."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
import re
import time

from .. import config, db, upstreamsvc
from . import wb2api

PREFIX = 'account-risk: '
_task: asyncio.Task | None = None


def initialize() -> int:
    db.execute('''CREATE TABLE IF NOT EXISTS account_risk_events (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, uid TEXT NOT NULL,
        nickname TEXT NOT NULL, realm TEXT NOT NULL, operation TEXT NOT NULL,
        http_status INTEGER NOT NULL, ts INTEGER NOT NULL, historical INTEGER NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0, action_error TEXT NOT NULL DEFAULT '',
        protection_active INTEGER NOT NULL DEFAULT 0)''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_account_risk_ts ON account_risk_events(ts)')
    with db._lock:
        enabled = db.get_setting('account_risk_enabled_at')
        if not isinstance(enabled, int) or enabled <= 0:
            enabled = int(time.time())
            db.set_setting('account_risk_enabled_at', enabled)
    return enabled


def scope_of(directory: Path | str | None) -> str:
    return str(Path(directory or config.AUTH_DIR).resolve())


def _groups() -> list[dict]:
    return [g for g in upstreamsvc.list_upstreams() if g.get('auth_dir')]


def _group(directory: Path | str | None) -> dict | None:
    scope = scope_of(directory)
    return next((g for g in _groups() if scope_of(g['auth_dir']) == scope), None)


def is_risk_pause(account: dict) -> bool:
    return account.get('manual_disabled') is True and str(account.get('manual_reason') or '').startswith(PREFIX)


def record(directory: Path | str | None, account: dict, operation: str, status: int,
           ts: int, *, historical: bool = False) -> tuple[str, bool]:
    initialize()
    scope, uid = scope_of(directory), str(account.get('uid') or '')
    ident = hashlib.sha256(f'{scope}\0{uid}\0{operation}\0{status}\0{ts}'.encode()).hexdigest()[:32]
    with db._lock:
        conn = db.connect()
        cursor = conn.execute('''INSERT OR IGNORE INTO account_risk_events
            (id,scope,uid,nickname,realm,operation,http_status,ts,historical)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (ident, scope, uid, str(account.get('nickname') or uid), account.get('realm') or 'cn',
             operation, status, ts, int(historical)))
        conn.commit()
        return ident, cursor.rowcount == 1


async def paused(uid: str, directory: Path | str | None) -> bool:
    initialize()
    previous = db.query_one('SELECT MAX(protection_active) AS active FROM account_risk_events WHERE scope=? AND uid=?', (scope_of(directory), uid))
    protected = bool(previous and previous['active'])
    if protected and any(a.get('uid') == uid and a.get('disabled_by_panel') for a in wb2api.list_auth_accounts(Path(scope_of(directory)))):
        return True
    group = _group(directory)
    if group is None:
        return protected
    status = await wb2api.get_status(base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
    account = next((a for a in status.get('accounts') or [] if str(a.get('uid')) == uid), None)
    if not status.get('connected') or account is None:
        return protected
    if is_risk_pause(account):
        observe_pause(directory, account)
        return True
    db.execute('UPDATE account_risk_events SET protection_active=0 WHERE scope=? AND uid=?', (scope_of(directory), uid))
    return False


def observe_pause(directory: Path | str | None, account: dict) -> None:
    scope, uid = scope_of(directory), str(account['uid'])
    match = re.search(r'at=([^ ;]+)', str(account.get('manual_reason') or ''))
    existing = db.query_one('SELECT ts FROM account_risk_events WHERE scope=? AND uid=? AND operation=? ORDER BY ts DESC LIMIT 1', (scope, uid, 'token_refresh'))
    try:
        ts = int(datetime.fromisoformat(match.group(1).replace('Z', '+00:00')).timestamp()) if match else (existing['ts'] if existing else int(time.time()))
    except ValueError:
        ts = existing['ts'] if existing else int(time.time())
    record(scope, account, 'token_refresh', 429, ts)
    db.execute('UPDATE account_risk_events SET protection_active=1 WHERE scope=? AND uid=?', (scope, uid))


async def paused_targets(directory: Path | str | None) -> set[str]:
    initialize()
    scope = scope_of(directory)
    protected = {r['uid'] for r in db.query('SELECT DISTINCT uid FROM account_risk_events WHERE scope=? AND protection_active=1', (scope,))}
    file_paused = {a['uid'] for a in wb2api.list_auth_accounts(Path(scope)) if a.get('disabled_by_panel')}
    group = _group(directory)
    if group is None:
        return protected
    status = await wb2api.get_status(base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
    if status.get('connected'):
        for account in status.get('accounts') or []:
            uid = str(account.get('uid') or '')
            if is_risk_pause(account):
                observe_pause(directory, account)
                protected.add(uid)
            elif uid not in file_paused:
                protected.discard(uid)
                db.execute('UPDATE account_risk_events SET protection_active=0 WHERE scope=? AND uid=?', (scope, uid))
    return protected


async def pause_refresh(uid: str, directory: Path | str | None, *, ts: int | None = None) -> bool:
    """Pause only this account. Older upstreams can use reversible file isolation."""
    now = int(time.time()) if ts is None else ts
    accounts = wb2api.list_auth_accounts(Path(scope_of(directory)))
    account = next((a for a in accounts if a.get('uid') == uid), None)
    if account is None:
        return False
    ident, inserted = record(directory, account, 'token_refresh', 429, now)
    if not inserted:
        already_paused = await paused(uid, directory)
        if already_paused or ts is not None:
            return already_paused
        # A new live attempt after explicit enable can fail in the same second.
        # Deduplicate replayed log events, not a genuinely new refresh response.
        db.execute('UPDATE account_risk_events SET acknowledged=0, action_error=? WHERE id=?', ('', ident))
    group = _group(directory)
    if group is None:
        db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?', ('group_unavailable', ident))
        return False
    ok, _, code = await wb2api.set_manual_disabled(uid, True, PREFIX + 'token_refresh HTTP 429; manual review required',
        base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
    if ok:
        confirmed = await paused(uid, directory)
        if not confirmed:
            db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?', ('pause_unconfirmed', ident))
        return confirmed
    if code == 'no_route':
        try:
            wb2api.set_account_disabled(account['file'], True, Path(scope_of(directory)))
            db.execute('UPDATE account_risk_events SET protection_active=1 WHERE id=?', (ident,))
            # Modern upstreams reload auth files within 5 seconds. No restart or global stop.
            return True
        except (OSError, ValueError):
            code = 'file_pause_failed'
    db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?', (code, ident))
    return False


def classify_task(event: dict) -> tuple[str, int] | None:
    message, kind = str(event.get('message') or ''), event.get('kind')
    if kind not in ('keepalive', 'checkin', 'travel', 'activity', 'user-resource'):
        return None
    if re.search(r'\b429\b', message):
        return ('token_refresh' if kind == 'keepalive' or 'refresh:' in message else str(kind), 429)
    server_error = re.search(r'http (5\d\d)', message, re.I)
    if server_error or 'grant credits failed' in message:
        return str(kind), int(server_error.group(1)) if server_error else 500
    return None


async def handle_tasks(events: list[dict]) -> None:
    enabled = initialize()
    directory = config.AUTH_DIR
    accounts = wb2api.list_auth_accounts(directory)
    for event in events:
        classified = classify_task(event)
        if not classified or not event.get('uid'):
            continue
        # Logs use uid8. Never guess when multiple accounts share a prefix.
        matches = [a for a in accounts if str(a.get('uid') or '').startswith(str(event['uid']))]
        operation, status = classified
        ts = int(event.get('ts') or 0)
        if ts <= 0:
            continue
        if len(matches) != 1:
            ident, _ = record(directory, {'uid':str(event['uid'])}, operation, status, ts, historical=ts < enabled)
            db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?',
                       ('account_id_ambiguous' if matches else 'account_not_found', ident))
            continue
        account = matches[0]
        if operation == 'token_refresh' and status == 429 and ts >= enabled:
            await pause_refresh(account['uid'], directory, ts=ts)
        else:
            record(directory, account, operation, status, ts, historical=ts < enabled)


async def warnings(*, group_id: int | None = None, all_groups: bool = False, realm: str | None = None) -> dict:
    initialize()
    groups = _groups() if all_groups else [upstreamsvc.default_upstream() if group_id is None else upstreamsvc.get_upstream(group_id)]
    items, failed_groups = [], []
    for group in groups:
        if not group or not group.get('auth_dir'):
            continue
        scope = scope_of(group['auth_dir'])
        status = await wb2api.get_status(base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
        known = status.get('connected') is True
        if not known:
            failed_groups.append(group['name'])
        pool = {str(a.get('uid')): a for a in status.get('accounts') or []}
        local = {a['uid']: a for a in wb2api.list_auth_accounts(Path(scope))}
        for uid, account in pool.items():
            if is_risk_pause(account):
                observe_pause(scope, {**local.get(uid, {}), **account})
            elif known and not local.get(uid, {}).get('disabled_by_panel'):
                db.execute('UPDATE account_risk_events SET protection_active=0 WHERE scope=? AND uid=?', (scope, uid))
        rows = db.query('SELECT * FROM account_risk_events WHERE scope=? ORDER BY ts DESC LIMIT 200', (scope,))
        seen_paused = set()
        for row in rows:
            if realm in ('cn', 'global') and row['realm'] != realm:
                continue
            account = pool.get(row['uid'], {})
            file_paused = bool(row['protection_active']) and local.get(row['uid'], {}).get('disabled_by_panel') is True
            token_warning = row['operation'] == 'token_refresh' and row['http_status'] == 429
            active = ((is_risk_pause(account) or file_paused) and token_warning) if known else None
            if row['acknowledged'] and active is False:
                continue
            if active is True and row['uid'] in seen_paused:
                continue
            if active is True:
                seen_paused.add(row['uid'])
            items.append({k: row[k] for k in ('id', 'uid', 'nickname', 'realm', 'operation', 'http_status', 'ts', 'action_error')} |
                         {'paused': active, 'group': group['name'], 'historical': bool(row['historical']), 'acknowledged': bool(row['acknowledged'])})
    return {'items': sorted(items, key=lambda x: x['ts'], reverse=True), 'failed_groups': failed_groups}


def acknowledge(ident: str) -> bool:
    initialize()
    with db._lock:
        conn = db.connect()
        cursor = conn.execute('UPDATE account_risk_events SET acknowledged=1 WHERE id=?', (ident,))
        conn.commit()
        return cursor.rowcount == 1


async def _loop() -> None:
    initialize()
    # Import old failures as warnings only. Historical logs must never re-pause accounts.
    rows = db.query("SELECT * FROM task_logs WHERE ts>=? AND kind IN ('keepalive','checkin','travel','activity','user-resource') AND (message LIKE '%429%' OR lower(message) LIKE '%http 5%' OR message LIKE '%grant credits failed%') ORDER BY ts DESC LIMIT 500", (int(time.time())-48*3600,))
    await handle_tasks([dict(r) for r in rows])
    while True:
        try:
            await warnings(all_groups=True)
        except Exception:
            # The API still reports failures to the UI; do not affect forwarding.
            import logging
            logging.getLogger(__name__).exception('账号保护预警采集失败')
        await asyncio.sleep(30)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


def stop() -> None:
    if _task:
        _task.cancel()
