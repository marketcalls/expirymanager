/**
 * The Fyers login opens a tab synchronously on the click, then navigates it once the authorize
 * URL comes back from the backend. That two step shape exists because a window.open issued after
 * an await has lost the user gesture and is blocked, but it only works if the handle survives.
 *
 * This file guards the one mistake that silently breaks it: passing noopener. That feature makes
 * window.open return null by specification, so the handle is lost and the tab that was just
 * opened sits blank forever while the backend reports a perfectly healthy 200.
 */

import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

const source = fs.readFileSync(
  path.join(import.meta.dirname, 'BrokerPanel.tsx'),
  'utf8',
)

describe('the Fyers connect tab', () => {
  it('does not ask for noopener on the tab it needs to navigate', () => {
    // Matches window.open(...) calls and checks none of them request noopener. The feature is
    // correct on a plain link, so this is scoped to window.open rather than the whole file.
    const calls = source.match(/window\.open\([^)]*\)/g) ?? []
    expect(calls.length).toBeGreaterThan(0)
    for (const call of calls) {
      expect(call).not.toContain('noopener')
    }
  })

  it('opens the tab synchronously rather than after the request resolves', () => {
    // The open must sit in the click handler, not in onSuccess, or the gesture is gone.
    const start = source.slice(source.indexOf('const start = useCallback'))
    const body = start.slice(0, start.indexOf('}, [connect])'))
    // Comments are stripped first. The comment above the call explains why an await here would be
    // wrong, and matching that prose would fail the test for saying the right thing.
    const code = body.replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '')
    expect(code).toContain('window.open')
    expect(code).not.toContain('await')
  })

  it('clears the opener reference instead of relying on noopener', () => {
    expect(source).toContain('tab.opener = null')
  })

  it('still surfaces the authorize url so a blocked popup is recoverable', () => {
    // When the browser blocks the popup outright the handle is null, and the only way through is
    // a link the user can click.
    expect(source).toContain('setAuthorizeUrl(data.authorize_url)')
  })
})
