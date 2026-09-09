import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { apiErrorMessage, isRouteMissing } from '@/components/settings/BrokerPanel'
import { EmptyState } from '@/components/common/EmptyState'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { SettingDescriptor } from '@/lib/api/types'

// The replacement for a .env file.
//
// Nothing here is hard coded. The backend ships the spec with the value (type, default, range,
// choices, whether a restart is needed) and the control is chosen from the spec, so a setting
// added on the backend appears here without a frontend change. That is the whole point of
// shipping the descriptor rather than just the value.

type SettingValue = string | number | boolean | null

/** The route has not been written yet at the time this screen was, and the documented shape is
 *  a bare array. A body that arrives wrapped in the usual paging envelope is accepted too rather
 *  than rendering an empty screen over a shape difference. */
function asDescriptors(body: unknown): SettingDescriptor[] {
  if (Array.isArray(body)) {
    return body as SettingDescriptor[]
  }
  if (body && typeof body === 'object' && Array.isArray((body as { items?: unknown }).items)) {
    return (body as { items: SettingDescriptor[] }).items
  }
  return []
}

/** Settings are named prefix_rest, so the prefix is the grouping the backend already chose. */
function groupOf(key: string): string {
  const cut = key.indexOf('_')
  return cut > 0 ? key.slice(0, cut) : key
}

function label(key: string): string {
  return key.replace(/_/g, ' ')
}

export function DataPanel() {
  const client = useQueryClient()
  const [edits, setEdits] = useState<Record<string, SettingValue>>({})

  const settings = useQuery({
    queryKey: queryKeys.system.settings(),
    queryFn: () => api.get<unknown>('/system/settings'),
    retry: false,
  })

  const descriptors = useMemo(() => asDescriptors(settings.data), [settings.data])

  const groups = useMemo(() => {
    const order: string[] = []
    const grouped = new Map<string, SettingDescriptor[]>()
    for (const descriptor of descriptors) {
      const group = groupOf(descriptor.key)
      if (!grouped.has(group)) {
        grouped.set(group, [])
        order.push(group)
      }
      grouped.get(group)?.push(descriptor)
    }
    return order.map((group) => ({ group, items: grouped.get(group) ?? [] }))
  }, [descriptors])

  const save = useMutation({
    mutationFn: (body: Record<string, SettingValue>) =>
      api.patch<unknown>('/system/settings', { body }),
    onSuccess: () => {
      setEdits({})
      void client.invalidateQueries({ queryKey: queryKeys.system.settings() })
      toast.success('Settings saved')
    },
  })

  if (settings.isError) {
    return (
      <EmptyState
        title={isRouteMissing(settings.error) ? 'Settings are not wired up yet' : 'Cannot read the settings'}
        description={
          isRouteMissing(settings.error)
            ? 'This build does not serve the settings route yet. Nothing is wrong with your installation, and the login and download paths do not depend on it.'
            : (apiErrorMessage(settings.error) ?? 'The backend returned an unexpected response.')
        }
        action={
          <Button size="sm" variant="outline" onClick={() => void settings.refetch()}>
            Retry
          </Button>
        }
      />
    )
  }

  if (settings.isPending) {
    return <p className="text-sm text-muted-foreground">Reading the settings.</p>
  }

  if (descriptors.length === 0) {
    return <EmptyState title="No settings" description="The backend reported no typed settings." />
  }

  const dirtyKeys = Object.keys(edits)
  const errorMessage = apiErrorMessage(save.error)

  const valueOf = (descriptor: SettingDescriptor): SettingValue =>
    descriptor.key in edits ? edits[descriptor.key] : descriptor.value

  const setValue = (key: string, value: SettingValue) =>
    setEdits((current) => ({ ...current, [key]: value }))

  return (
    <div className="flex flex-col gap-4">
      {groups.map(({ group, items }) => (
        <Card key={group}>
          <CardHeader>
            <CardTitle className="capitalize">{label(group)}</CardTitle>
            <CardDescription>
              {items.length === 1 ? '1 setting' : String(items.length) + ' settings'}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {items.map((descriptor) => {
              const current = valueOf(descriptor)
              const dirty = descriptor.key in edits
              return (
                <div
                  key={descriptor.key}
                  className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 border-b pb-3 last:border-b-0 last:pb-0"
                >
                  <div className="min-w-0 max-w-prose">
                    <p className="font-mono text-xs">{descriptor.key}</p>
                    <p className="text-sm text-muted-foreground">{descriptor.description}</p>
                    {descriptor.requires_restart ? (
                      <p className="text-xs text-muted-foreground">
                        Takes effect after the app is restarted.
                      </p>
                    ) : null}
                    {dirty ? <p className="text-xs">Changed, not saved yet.</p> : null}
                  </div>
                  <div className="w-56 shrink-0">
                    {descriptor.value_type === 'bool' ? (
                      <Switch
                        checked={Boolean(current)}
                        onCheckedChange={(next) => setValue(descriptor.key, next)}
                        aria-label={descriptor.key}
                      />
                    ) : descriptor.choices && descriptor.choices.length > 0 ? (
                      <Select
                        value={String(current ?? '')}
                        onValueChange={(next) => setValue(descriptor.key, next)}
                      >
                        <SelectTrigger className="w-full" aria-label={descriptor.key}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {descriptor.choices.map((choice) => (
                            <SelectItem key={choice} value={choice}>
                              {choice}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : descriptor.value_type === 'int' || descriptor.value_type === 'float' ? (
                      <Input
                        type="number"
                        aria-label={descriptor.key}
                        value={current === null ? '' : String(current)}
                        min={descriptor.minimum ?? undefined}
                        max={descriptor.maximum ?? undefined}
                        step={descriptor.value_type === 'int' ? 1 : 'any'}
                        onChange={(event) => {
                          const raw = event.target.value
                          setValue(descriptor.key, raw === '' ? null : Number(raw))
                        }}
                      />
                    ) : (
                      <Input
                        aria-label={descriptor.key}
                        value={current === null ? '' : String(current)}
                        onChange={(event) => setValue(descriptor.key, event.target.value)}
                      />
                    )}
                    <p className="mt-1 text-xs text-muted-foreground">
                      Default {String(descriptor.default ?? 'none')}
                      {descriptor.minimum !== null || descriptor.maximum !== null
                        ? ', range ' +
                          String(descriptor.minimum ?? 'any') +
                          ' to ' +
                          String(descriptor.maximum ?? 'any')
                        : ''}
                    </p>
                  </div>
                </div>
              )
            })}
          </CardContent>
        </Card>
      ))}

      {errorMessage ? (
        <p role="alert" className="text-sm text-destructive">
          {errorMessage}
        </p>
      ) : null}

      <div className="flex items-center gap-3">
        <Button
          size="sm"
          disabled={dirtyKeys.length === 0 || save.isPending}
          onClick={() => save.mutate(edits)}
        >
          {save.isPending ? 'Saving' : 'Save changes'}
        </Button>
        {dirtyKeys.length > 0 ? (
          <Button size="sm" variant="ghost" onClick={() => setEdits({})}>
            Discard
          </Button>
        ) : null}
        <span className="text-xs text-muted-foreground">
          {dirtyKeys.length === 0
            ? 'No unsaved changes.'
            : String(dirtyKeys.length) + ' unsaved.'}
        </span>
      </div>
    </div>
  )
}

export default DataPanel
