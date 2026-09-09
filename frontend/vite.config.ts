import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The backend generates this pair on first run. Reusing it means the browser is asked to
// trust one self-signed certificate for both origins instead of two.
const tlsDir = path.join(os.homedir(), '.expirymanager', 'tls')
const keyFile = path.join(tlsDir, 'server.key')
const certFile = path.join(tlsDir, 'server.crt')

function devHttps(): { key: Buffer; cert: Buffer } | undefined {
  try {
    return { key: fs.readFileSync(keyFile), cert: fs.readFileSync(certFile) }
  } catch {
    // Frontend-only work should still start. The backend has not run yet, or the pair was
    // removed for renewal. Plain http here breaks the shared-cookie story, so say so once.
    console.warn(
      '[expirymanager] no certificate at ' +
        tlsDir +
        ', serving the dev server over http. ' +
        'Start the backend once to generate it, then restart this dev server.',
    )
    return undefined
  }
}

// The config is a function so the certificate is only read for `vite` and never for
// `vite build`, which would otherwise print the missing-certificate warning on every build.
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  resolve: {
    // import.meta.dirname rather than __dirname: this config is ESM.
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    // 127.0.0.1 and not localhost. Cookies ignore the port but not the host, so the session
    // cookie the OAuth callback sets on 127.0.0.1:8000 is only visible here under the same host.
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    https: command === 'serve' ? devHttps() : undefined,
    proxy: {
      '/api': {
        target: 'https://127.0.0.1:8000',
        // changeOrigin stays false so the browser Origin header survives to FastAPI, which
        // checks it on unsafe methods.
        changeOrigin: false,
        // The backend certificate is self-signed. This disables verification for this one
        // loopback target only.
        secure: false,
      },
    },
  },
}))
