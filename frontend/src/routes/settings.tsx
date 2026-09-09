import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { BrokerPanel } from '@/components/settings/BrokerPanel'
import { DataPanel } from '@/components/settings/DataPanel'
import { DiagnosticsPanel } from '@/components/settings/DiagnosticsPanel'
import { SecurityPanel } from '@/components/settings/SecurityPanel'
import { StoragePanel } from '@/components/settings/StoragePanel'
import { PageHeader } from '@/components/common/PageHeader'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, BrokerStatus } from '@/lib/api/types'

// Settings is also the landing pad for the OAuth callback. The backend redirects here with
// ?broker=connected or ?broker=failed&reason=..., in the tab that was sent to Fyers, and that
// tab may never have shown this screen before.
//
// So the result of the login is announced here, in words, with the next step attached. The five
// failure reasons are the five the backend can send and no others; each one names what went
// wrong and what to do about it, because a login that fails with a bare code is a login the user
// cannot finish.

const TAB_PARAM = 'tab'
const BROKER_PARAM = 'broker'
const REASON_PARAM = 'reason'

const TABS = [
  { value: 'broker', label: 'Broker' },
  { value: 'security', label: 'Security' },
  { value: 'data', label: 'Data' },
  { value: 'storage', label: 'Storage' },
  { value: 'diagnostics', label: 'Diagnostics' },
] as const

type TabValue = (typeof TABS)[number]['value']

function isTab(value: string | null): value is TabValue {
  return TABS.some((tab) => tab.value === value)
}

/** The five reasons api/oauth_callback.py can redirect with, and what to do about each. */
export const CALLBACK_REASONS: Record<string, string> = {
  state_invalid:
    'The return could not be matched to the login this app started. That happens when the link ' +
    'was opened more than ten minutes after Connect was pressed, when it had already been used ' +
    'once, or when it was opened in a different browser than the one that started it. Press ' +
    'Connect again and finish the login in the tab it opens.',
  login_failed:
    'Fyers sent you back without an auth code, which means the sign in was not completed there. ' +
    'Press Connect again and complete the Fyers password and TOTP step.',
  exchange_failed:
    'Fyers rejected the exchange of that login for a token. The usual cause is an app id or app ' +
    'secret that does not match the registration, or a redirect URL registered with even one ' +
    'character different. Check the app registration below, then connect again.',
  no_credentials:
    'There is no app id and secret stored, so there was nothing to exchange the login with. ' +
    'Save the credentials below, then connect.',
  unexpected:
    'Something failed while completing the login. Press Connect and try once more. If it repeats, ' +
    'the log file in the data directory carries the detail under the correlation id it printed.',
}

export function SettingsRoute() {
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()

  const requestedTab = params.get(TAB_PARAM)
  const tab: TabValue = isTab(requestedTab) ? requestedTab : 'broker'
  const callbackResult = params.get(BROKER_PARAM)
  const callbackReason = params.get(REASON_PARAM)

  const bootstrap = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: () => api.get<Bootstrap>('/bootstrap'),
    staleTime: Infinity,
    retry: false,
  })

  const broker = useQuery({
    queryKey: queryKeys.broker.fyers(),
    queryFn: () => api.get<BrokerStatus>('/broker/fyers'),
    retry: false,
  })

  // The callback changed the token in a different request than any this tab made, so whatever is
  // cached here is stale by construction.
  useEffect(() => {
    if (callbackResult) {
      void client.invalidateQueries({ queryKey: queryKeys.broker.fyers() })
      void client.invalidateQueries({ queryKey: queryKeys.bootstrap() })
    }
  }, [callbackResult, client])

  const dismissCallback = () => {
    const next = new URLSearchParams(params)
    next.delete(BROKER_PARAM)
    next.delete(REASON_PARAM)
    setParams(next, { replace: true })
  }

  const selectTab = (value: string) => {
    const next = new URLSearchParams(params)
    if (value === 'broker') {
      next.delete(TAB_PARAM)
    } else {
      next.set(TAB_PARAM, value)
    }
    setParams(next, { replace: true })
  }

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        title="Settings"
        description="Broker credentials, the local passcode, the typed settings that replace a .env file, storage and diagnostics."
      />

      <div className="flex flex-col gap-4 px-5 py-4">
        {callbackResult === 'connected' ? (
          <div
            role="status"
            className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 rounded-lg border border-chart-5/40 bg-chart-5/10 px-3 py-2.5"
          >
            <div className="min-w-0">
              <p className="text-sm font-medium">Fyers login completed</p>
              <p className="text-xs text-muted-foreground">
                A token is stored and encrypted. Any downloads that were parked awaiting
                authentication have been moved back into the queue and will resume on their own.
              </p>
            </div>
            <Button size="sm" variant="outline" onClick={dismissCallback}>
              Dismiss
            </Button>
          </div>
        ) : null}

        {callbackResult === 'failed' ? (
          <div
            role="alert"
            className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5"
          >
            <div className="min-w-0 max-w-prose">
              <p className="text-sm font-medium">The Fyers login did not complete</p>
              <p className="text-xs text-muted-foreground">
                {CALLBACK_REASONS[callbackReason ?? ''] ?? CALLBACK_REASONS.unexpected}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Nothing was lost. No token was stored, and parked downloads stay parked until a
                login lands.
              </p>
            </div>
            <Button size="sm" variant="outline" onClick={dismissCallback}>
              Dismiss
            </Button>
          </div>
        ) : null}

        <Tabs value={tab} onValueChange={selectTab}>
          <TabsList>
            {TABS.map((entry) => (
              <TabsTrigger key={entry.value} value={entry.value}>
                {entry.label}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="broker" className="mt-4">
            <BrokerPanel
              status={broker.data}
              isLoading={broker.isPending}
              error={broker.error}
            />
          </TabsContent>

          <TabsContent value="security" className="mt-4">
            <SecurityPanel bootstrap={bootstrap.data} brokerStatus={broker.data} />
          </TabsContent>

          <TabsContent value="data" className="mt-4">
            <DataPanel />
          </TabsContent>

          <TabsContent value="storage" className="mt-4">
            <StoragePanel />
          </TabsContent>

          <TabsContent value="diagnostics" className="mt-4">
            <DiagnosticsPanel bootstrap={bootstrap.data} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

export default SettingsRoute
