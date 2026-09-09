// Scaffold contract tests for W06.
//
// These assert the handful of settings that other work items code against and that nothing
// else in the tree would fail on if they silently regressed: the dev origin, the proxy target,
// the path alias, and the route table.

import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import viteConfig from '../vite.config'
import { NAV_ITEMS } from './App'

const root = path.resolve(import.meta.dirname, '..')

async function resolveConfig(command: 'serve' | 'build') {
  const value =
    typeof viteConfig === 'function'
      ? await (viteConfig as any)({ command, mode: 'development' })
      : viteConfig
  return value as Record<string, any>
}

describe('vite dev server', () => {
  it('serves the dev origin on 127.0.0.1:5173', async () => {
    const config = await resolveConfig('serve')
    // Not localhost. Cookies ignore the port but not the host, so this is what makes the
    // session cookie set on 127.0.0.1:8000 visible to the dev origin.
    expect(config.server.host).toBe('127.0.0.1')
    expect(config.server.port).toBe(5173)
    expect(config.server.strictPort).toBe(true)
  })

  it('serves plain http by default, matching the backend', async () => {
    // The dev origin must use the same scheme as the backend. The backend serves http because the
    // redirect URI registered with Fyers is http, and a Secure cookie is never sent over http, so
    // a dev server on https against an http backend breaks the session rather than hardening it.
    delete process.env.EXPIRYMANAGER_DEV_HTTPS
    const config = await resolveConfig('serve')
    expect(config.server.https).toBeUndefined()
  })

  it('serves https only when explicitly opted in and the certificate exists', async () => {
    process.env.EXPIRYMANAGER_DEV_HTTPS = '1'
    try {
      const config = await resolveConfig('serve')
      const present = fs.existsSync(
        path.join(process.env.HOME ?? '', '.expirymanager', 'tls', 'server.crt'),
      )
      if (present) {
        expect(config.server.https).toHaveProperty('cert')
      } else {
        // A missing certificate degrades to http rather than throwing, so the dev server still
        // starts on a machine where the backend has never run.
        expect(config.server.https).toBeUndefined()
      }
    } finally {
      delete process.env.EXPIRYMANAGER_DEV_HTTPS
    }
  })

  it('does not read the certificate during a build', async () => {
    const config = await resolveConfig('build')
    expect(config.server.https).toBeUndefined()
  })

  it('proxies /api to the backend over https with verification off', async () => {
    const config = await resolveConfig('serve')
    const api = config.server.proxy['/api']
    expect(api.target).toBe('http://127.0.0.1:8000')
    // The Origin header has to survive to FastAPI, which checks it on unsafe methods.
    expect(api.changeOrigin).toBe(false)
    // The backend certificate is self-signed, and this is the only target it applies to.
    expect(api.secure).toBe(false)
  })

  it('resolves the @ alias to src', async () => {
    const config = await resolveConfig('serve')
    expect(config.resolve.alias['@']).toBe(path.join(root, 'src'))
  })
})

describe('typescript config', () => {
  // The tsconfigs are JSONC. Regex comment stripping is not safe here: the alias value
  // "@/*" itself opens a block comment. Use the compiler's own reader instead.
  const read = (name: string) => {
    const result = ts.readConfigFile(path.join(root, name), (p) =>
      fs.readFileSync(p, 'utf8'),
    )
    expect(result.error, name).toBeUndefined()
    return result.config
  }

  it('declares the same @ alias that vite resolves', () => {
    expect(read('tsconfig.json').compilerOptions.paths['@/*']).toEqual(['./src/*'])
    expect(read('tsconfig.app.json').compilerOptions.paths['@/*']).toEqual(['./src/*'])
  })

  it('omits baseUrl, which is a hard TS5101 error on TypeScript 6 and 7', () => {
    for (const name of ['tsconfig.json', 'tsconfig.app.json', 'tsconfig.node.json']) {
      expect(read(name).compilerOptions.baseUrl).toBeUndefined()
    }
  })
})

describe('route table', () => {
  const modules = import.meta.glob('./routes/*.tsx', { eager: true }) as Record<
    string,
    { default?: unknown }
  >

  it('ships a stub for every screen the architecture names', () => {
    const names = Object.keys(modules)
      .map((p) => path.basename(p, '.tsx'))
      .sort()
    expect(names).toEqual([
      'chain',
      'chart',
      'contracts',
      'dashboard',
      'expiries',
      'exports',
      'job-detail',
      'jobs',
      'login',
      'schedules',
      'settings',
      'setup',
      'underlyings',
    ])
  })

  it('gives every route stub a default export component', () => {
    for (const [file, mod] of Object.entries(modules)) {
      expect(typeof mod.default, file).toBe('function')
    }
  })

  it('points every navigation entry at a distinct path', () => {
    const paths = NAV_ITEMS.map((item) => item.to)
    expect(new Set(paths).size).toBe(paths.length)
  })
})

describe('index.css', () => {
  const css = fs.readFileSync(path.join(root, 'src', 'index.css'), 'utf8')

  it('keeps the class based dark variant shadcn generated', () => {
    expect(css).toContain('@custom-variant dark (&:is(.dark *));')
    // A second definition would silently win or lose depending on order.
    expect(css.match(/@custom-variant dark /g)).toHaveLength(1)
  })

  it('marks the oac token overrides important, since the widget writes them inline', () => {
    const block = css.slice(css.indexOf('.oac-widget {'))
    expect(block).toContain('--oac-font')
    for (const line of block.split('\n').filter((l) => l.includes('--oac-'))) {
      expect(line, line).toContain('!important')
    }
  })

  it('styles scrollbars globally', () => {
    expect(css).toContain('::-webkit-scrollbar-thumb')
    expect(css).toContain('scrollbar-width: thin')
  })
})
