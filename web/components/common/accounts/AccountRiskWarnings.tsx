'use client';

import {useState} from 'react';
import Link from 'next/link';
import {ShieldAlert} from 'lucide-react';
import {accountRiskApi, errText} from '@/lib/api';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useRealm} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import {Button} from '@/components/ui/button';

export function AccountRiskWarnings({upstreamId = null, allGroups = false}: {
  upstreamId?: number | null; allGroups?: boolean;
}) {
  const {realm} = useRealm();
  const {isAdmin} = useAuth();
  const t = useT();
  const [busy, setBusy] = useState(false);
  const {values, errors, reload} = useAsyncAll({warnings: () => accountRiskApi.list(realm, upstreamId, allGroups)},
    [realm, upstreamId, allGroups]);
  useHeartbeat(reload, 30000);
  const data = values.warnings;
  const failed = !!errors.warnings || !!data?.failed_groups?.length;
  const items = data?.items ?? [];
  const pausedCount = new Set(items.filter(x => x.paused === true).map(x => `${x.group}:${x.uid}`)).size;
  if (!items.length && !failed) return null;

  async function acknowledge() {
    setBusy(true);
    try {
      for (const item of items.filter(item => !item.acknowledged)) await accountRiskApi.acknowledge(item.id);
      await reload();
      notify.ok(t('risk.acknowledged'));
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <section aria-label={t('risk.title')} className="rounded-2xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <ShieldAlert className="h-4 w-4 shrink-0 text-amber-600" />
        <h2 className="font-semibold">{t('risk.title')}</h2>
        <span className="text-xs text-muted-foreground">{t('risk.count', {n: new Set(items.map(x => `${x.group}:${x.uid}`)).size})}</span>
        {!failed && <span className="text-xs font-medium">{t('risk.pausedCount', {n: pausedCount})}</span>}
        {isAdmin && items.some(x => !x.acknowledged) && (
          <Button variant="outline" size="sm" className="ml-auto" disabled={busy} onClick={acknowledge}>{t('risk.acknowledge')}</Button>
        )}
      </div>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{t('risk.explanation')}</p>
      {!failed && pausedCount === 0 && items.every(x => x.paused === false) && <p className="mt-2 text-xs font-medium">{t('risk.noNewPauses')}</p>}
      {failed && <p role="alert" className="mt-2 text-xs text-red-600">{t('risk.loadFailed')} <Button variant="link" size="sm" onClick={reload}>{t('state.retry')}</Button></p>}
      <details className="mt-2" open={items.some(x => x.paused === true)}>
        <summary className="cursor-pointer text-xs font-medium">{t('risk.details')}</summary>
        <ul className="mt-2 max-h-72 space-y-2 overflow-y-auto">
          {items.map(item => (
            <li key={item.id} className="rounded-lg bg-background/60 p-2 text-xs leading-5">
              <div className="flex flex-wrap items-center gap-x-2">
                <span className="break-all font-medium">{item.nickname || item.uid}</span>
                <span className="text-muted-foreground">{item.group}</span>
                <strong>{t(item.paused === true ? 'risk.paused' : item.paused === null ? 'risk.unknown' : item.historical ? 'risk.historical' : 'risk.warning')}</strong>
              </div>
              <div>{t(item.operation === 'token_refresh' ? 'risk.refresh' : 'risk.background')} · HTTP {item.http_status} · {fmtDateTime(item.ts)}</div>
              {item.action_error && <p className="text-red-600">{t('risk.pauseFailed')} ({item.action_error})</p>}
            </li>
          ))}
        </ul>
      </details>
      <p className="mt-2 text-xs text-muted-foreground">{t('risk.recovery')} <Link href="/accounts" className="underline">{t('risk.accounts')}</Link></p>
    </section>
  );
}
