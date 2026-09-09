// @vitest-environment jsdom

// public/theme-init.js runs before the React tree exists, so it has no test seam of its own.
// These tests evaluate the shipped source against a controlled window, which is the only way
// to catch it drifting from next-themes' stored value vocabulary.

import fs from 'node:fs'
import path from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'

const source = fs.readFileSync(
  path.resolve(import.meta.dirname, '..', 'public', 'theme-init.js'),
  'utf8',
)

function run(stored: string | null, systemPrefersDark: boolean) {
  document.documentElement.className = ''
  document.documentElement.style.colorScheme = ''
  window.localStorage.clear()
  if (stored !== null) {
    window.localStorage.setItem('expirymanager-theme', stored)
  }
  window.matchMedia = ((query: string) => ({
    matches: query.includes('dark') && systemPrefersDark,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia

  // eslint-disable-next-line no-new-func
  new Function(source).call(window)
  return document.documentElement
}

beforeEach(() => {
  document.documentElement.className = ''
})

describe('theme-init', () => {
  it('honours an explicit dark choice over the system preference', () => {
    expect(run('dark', false).classList.contains('dark')).toBe(true)
  })

  it('honours an explicit light choice over the system preference', () => {
    expect(run('light', true).classList.contains('dark')).toBe(false)
  })

  it('follows the system preference when the stored value is system', () => {
    expect(run('system', true).classList.contains('dark')).toBe(true)
    expect(run('system', false).classList.contains('dark')).toBe(false)
  })

  it('follows the system preference on a first visit', () => {
    expect(run(null, true).classList.contains('dark')).toBe(true)
    expect(run(null, false).classList.contains('dark')).toBe(false)
  })

  it('sets colorScheme so form controls and scrollbars match', () => {
    expect(run('dark', false).style.colorScheme).toBe('dark')
    expect(run('light', true).style.colorScheme).toBe('light')
  })

  it('uses the same storage key the ThemeProvider is configured with', () => {
    const main = fs.readFileSync(
      path.resolve(import.meta.dirname, 'main.tsx'),
      'utf8',
    )
    expect(source).toContain("'expirymanager-theme'")
    expect(main).toContain('storageKey="expirymanager-theme"')
  })
})
