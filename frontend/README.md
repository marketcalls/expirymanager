# ExpiryManager frontend

React 19, Vite 8, Tailwind 4, TypeScript, shadcn (radix base, nova preset).

## Commands

```
npm install
npm run dev         # https://127.0.0.1:5173
npm run build       # tsc -b && vite build
npm run typecheck
npm run lint
npm test
```

## Why the dev server is https on 127.0.0.1

The Fyers redirect URI is registered as `https://127.0.0.1:8000/fyers/callback` and Fyers
matches it exactly, so the backend serves https on that host and port from a self-signed
certificate it generates on first run at `~/.expirymanager/tls/server.{key,crt}`.

The dev server reuses that same pair. The host must be `127.0.0.1` and not `localhost`:
cookies ignore the port but not the host, so same host on a different port is what makes
the session cookie the OAuth callback sets on port 8000 visible to the dev origin on 5173.

If the certificate is not there yet (the backend has never run), the dev server falls back
to http, prints a note and still starts, so frontend-only work is not blocked. The session
cookie will not be shared in that mode.

`/api` is proxied to `https://127.0.0.1:8000` with `secure: false`, because the backend
certificate is self-signed, and `changeOrigin: false`, because FastAPI checks the browser
`Origin` header on unsafe methods.

## Config notes

- Tailwind 4 is configured through `@tailwindcss/vite` and CSS-first tokens in
  `src/index.css`. There is no `tailwind.config.js` and no PostCSS chain.
- The `@` alias is declared in `vite.config.ts`, `tsconfig.json` and `tsconfig.app.json`.
  None of them sets `baseUrl`: on TypeScript 6 and 7 that is a hard TS5101 error, and
  `paths` resolves relative to the tsconfig directory without it.
- Theme is next-themes with `attribute="class"`, storage key `expirymanager-theme`, which
  matches the `@custom-variant dark (&:is(.dark *))` line shadcn wrote into `src/index.css`.
  `public/theme-init.js` sets the class before first paint. It is a file and not an inline
  script because the production CSP is `script-src 'self'`.
- Chart components read `resolvedTheme` from next-themes and call `widget.setTheme(...)`,
  so the openalgo-charts canvas and the app chrome move together off one source of truth.
- `src/routes/*.tsx` are placeholder stubs. Phase 4 replaces them in place; the paths in
  `src/App.tsx` are the contract.
