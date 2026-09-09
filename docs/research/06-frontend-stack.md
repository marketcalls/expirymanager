# ExpiryManager frontend stack: verified setup

Research date: 2026-09-09. Every version below was read from the live npm registry on
that date, and the whole stack was scaffolded, installed, type checked, built and
smoke tested end to end in a throwaway project before this note was written. Where a
statement is not backed by a command I actually ran, it is marked UNVERIFIED.

House rules applied here and to all frontend code: no emoji or icon glyphs in code,
comments, commits, docs, tests or terminal output; no em dashes or en dashes; comments
explain why, not what.

## 1. What was actually verified

A scratch project was created and driven all the way through:

1. `npm create vite@latest fe-test -- --template react-ts --yes` (create-vite 9.2.0).
2. `npm install tailwindcss @tailwindcss/vite`.
3. Path aliases added to `vite.config.ts`, `tsconfig.json`, `tsconfig.app.json`.
4. `npx shadcn@4.21.0 init -b radix -p nova --no-monorepo -y`.
5. `npx shadcn@4.21.0 add` for 38 components in a single call.
6. `npm install @tanstack/react-query @tanstack/react-table react-hook-form zod @hookform/resolvers @tanstack/react-query-devtools`.
7. An `App.tsx` exercising TanStack Query polling, TanStack Table v9 with sorting,
   filtering, pagination and row selection features, and eight shadcn components.
8. `npm run build` (`tsc -b && vite build`) with both TypeScript 6.0.3 and 7.0.2.
   Result with TS 7.0.2: 2111 modules transformed, built in 156 ms, no errors.
9. `vite` dev server with a proxy to a local Python HTTP server, then curl through the
   proxy for JSON, `Set-Cookie` and a Server Sent Events stream.

So the numbers and files in this note are not from memory.

## 2. Versions (npm latest, 2026-09-09)

| Package | latest | Notes |
| --- | --- | --- |
| vite | 8.2.2 | engines: `^20.19.0 \|\| >=22.12.0`. Node 26.4.0 is fine. |
| @vitejs/plugin-react | 6.1.1 | peer `vite: ^8.0.0`. Plugin 6 is the Vite 8 line. |
| react | 19.2.8 | |
| react-dom | 19.2.8 | |
| tailwindcss | 4.3.3 | |
| @tailwindcss/vite | 4.3.3 | peer `vite: ^5.2.0 \|\| ^6 \|\| ^7 \|\| ^8` |
| shadcn (CLI and runtime) | 4.21.0 | engines: node >= 20.18.1 |
| @tanstack/react-query | 5.102.8 | still v5, no v6 exists |
| @tanstack/react-table | 9.2.4 | v9.0.0 shipped 2026-08-04, this is new |
| typescript | 7.0.2 | create-vite still scaffolds `~6.0.2` |
| @types/react | 19.2.18 | |
| @types/react-dom | 19.2.7 | |
| @types/node | 26.5.0 | create-vite pins `^24.13.3`, either works |
| react-hook-form | 7.87.0 | |
| zod | 4.5.4 | |
| @hookform/resolvers | 5.9.1 | |
| lucide-react | 1.43.0 | |
| tw-animate-css | 1.4.0 | |
| cn | 0.2.6 | replaces clsx + tailwind-merge in shadcn 4 |
| sonner | 2.0.8 | |
| next-themes | 0.4.6 | framework agnostic despite the name |
| radix-ui | 1.6.7 | single package, not per primitive |
| @base-ui/react | 1.8.0 | pulled in by some components even on the radix base |
| openalgo-charts | 2.1.0 | published on npm, matches the local checkout version |

### Vite 8 and Tailwind 4 compatibility

No incompatibility. `@tailwindcss/vite@4.3.3` declares `vite: ^5.2.0 || ^6 || ^7 || ^8`
as a peer, and the full build succeeded on Vite 8.2.2 producing a 118.67 kB CSS bundle
(18.17 kB gzip) from the shadcn token sheet plus utilities.

Vite 8 ships Rolldown (Rust) as the single unified bundler, replacing the esbuild plus
Rollup split, and adds lightningcss for CSS minification. Minimum Node is unchanged from
Vite 7. The Tailwind Vite plugin is a normal Vite plugin and works through the Rolldown
plugin compatibility layer.

## 3. Scaffold commands (exact)

```bash
cd /Users/openalgo/AIBootcamp2026/Day26/ExpiryManager
npm create vite@latest frontend -- --template react-ts --yes
cd frontend
npm install
npm install tailwindcss @tailwindcss/vite
# then apply the config files in section 4, then:
npx shadcn@latest init -b radix -p nova --no-monorepo -y
npx shadcn@latest add button card table badge input label checkbox select \
  dialog alert-dialog dropdown-menu tabs progress sonner tooltip popover \
  command field separator scroll-area skeleton sheet alert calendar combobox \
  spinner empty sidebar pagination switch textarea toggle-group radio-group \
  breadcrumb input-group item kbd -y
npm install @tanstack/react-query @tanstack/react-table react-hook-form zod \
  @hookform/resolvers
npm install -D @tanstack/react-query-devtools
npm install openalgo-charts
```

Notes on what create-vite 9.2.0 actually produces:

- `oxlint` as the linter, not ESLint. `"lint": "oxlint"` and a `.oxlintrc.json`.
- `typescript: ~6.0.2`, `vite: ^8.2.2`, `@vitejs/plugin-react: ^6.1.0`,
  `@types/node: ^24.13.3`.
- Three tsconfigs: `tsconfig.json` (solution file with references),
  `tsconfig.app.json`, `tsconfig.node.json`. Neither app nor node config has `baseUrl`.
- `"build": "tsc -b && vite build"`.

There is also a one shot path, `npx shadcn@latest init -t vite`, which scaffolds the
Vite project and shadcn together. Use it only for a brand new directory. The staged form
above is preferable here because the aliases and the proxy config have to be written
anyway.

### shadcn CLI flags that matter (from `shadcn init --help`, run locally)

```
-t, --template <template>  next, start, vite, react-router, laravel, astro
-b, --base <base>          base, radix, aria
-p, --preset [name]        nova, vega, maia, lyra, mira, luma, sera, rhea
    --monorepo / --no-monorepo
-y, --yes                  default true
-d, --defaults             equals --template=next --preset=base-nova
    --css-variables / --no-css-variables   default true
    --rtl / --no-rtl
    --pointer / --no-pointer
-f, --force, -c --cwd, -n --name, -s --silent
```

`-b` plus `-p` compose into the `style` field: `-b radix -p nova` writes
`"style": "radix-nova"`. Passing `-p base-nova` is rejected: the CLI answers
`Invalid preset: base-nova. Available presets: nova, vega, maia, lyra, mira, luma,
sera, rhea`.

## 4. Exact config files

Tailwind 4 has no `tailwind.config.js` and no `postcss.config.js`. Configuration is
CSS first: the `@tailwindcss/vite` plugin plus `@import "tailwindcss"` plus an
`@theme` (or `@theme inline`) block. Confirmed against the official Vite install page
and against the working project.

### frontend/vite.config.ts

```ts
import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Cookie auth needs one browser origin, so the dev server fronts FastAPI.
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
```

`import.meta.dirname` is used instead of `__dirname` because the config is ESM.
The shadcn docs still show `path.resolve(__dirname, "./src")`; that requires the
CommonJS shim and is not needed on Node 26.

### frontend/tsconfig.json

```json
{
  "files": [],
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ],
  "compilerOptions": {
    "paths": { "@/*": ["./src/*"] }
  }
}
```

### frontend/tsconfig.app.json (created by create-vite, plus the paths line)

```json
{
  "compilerOptions": {
    "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.app.tsbuildinfo",
    "target": "es2023",
    "lib": ["ES2023", "DOM"],
    "module": "esnext",
    "types": ["vite/client"],
    "allowArbitraryExtensions": true,
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "verbatimModuleSyntax": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",
    "paths": { "@/*": ["./src/*"] },
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "erasableSyntaxOnly": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src"]
}
```

Critical correction to the shadcn documentation. Its Vite page tells you to add
`"baseUrl": "."` alongside `paths`. On TypeScript 6 and 7 that is a hard error:

```
tsconfig.app.json(18,5): error TS5101: Option 'baseUrl' is deprecated and will stop
functioning in TypeScript 7.0. Specify compilerOption '"ignoreDeprecations": "6.0"'
to silence this error.
```

Omit `baseUrl` entirely. `paths` resolves relative to the tsconfig directory without
it, the build passes, and the shadcn CLI still validates the alias: `shadcn add
accordion` was run against the baseUrl-free config and reported
`Validating import alias` OK and wrote the file.

### frontend/src/index.css (written by `shadcn init`, abridged token list)

```css
@import "tailwindcss";
@import "tw-animate-css";
@import "shadcn/tailwind.css";
@import "@fontsource-variable/geist";

@custom-variant dark (&:is(.dark *));

@theme inline {
    --font-heading: var(--font-sans);
    --font-sans: 'Geist Variable', sans-serif;
    --color-background: var(--background);
    --color-foreground: var(--foreground);
    --color-card: var(--card);
    --color-card-foreground: var(--card-foreground);
    --color-popover: var(--popover);
    --color-popover-foreground: var(--popover-foreground);
    --color-primary: var(--primary);
    --color-primary-foreground: var(--primary-foreground);
    --color-secondary: var(--secondary);
    --color-secondary-foreground: var(--secondary-foreground);
    --color-muted: var(--muted);
    --color-muted-foreground: var(--muted-foreground);
    --color-accent: var(--accent);
    --color-accent-foreground: var(--accent-foreground);
    --color-destructive: var(--destructive);
    --color-border: var(--border);
    --color-input: var(--input);
    --color-ring: var(--ring);
    --color-chart-1: var(--chart-1);
    /* chart-2 .. chart-5 and the sidebar-* group follow the same pattern */
    --radius-sm: calc(var(--radius) * 0.6);
    --radius-md: calc(var(--radius) * 0.8);
    --radius-lg: var(--radius);
    --radius-xl: calc(var(--radius) * 1.4);
    --radius-2xl: calc(var(--radius) * 1.8);
    --radius-3xl: calc(var(--radius) * 2.2);
    --radius-4xl: calc(var(--radius) * 2.6);
}

:root {
    --background: oklch(1 0 0);
    --foreground: oklch(0.145 0 0);
    --primary: oklch(0.205 0 0);
    --primary-foreground: oklch(0.985 0 0);
    --destructive: oklch(0.577 0.245 27.325);
    --border: oklch(0.922 0 0);
    --input: oklch(0.922 0 0);
    --ring: oklch(0.708 0 0);
    --radius: 0.625rem;
    /* full neutral base-colour set, card, popover, secondary, muted, accent,
       chart-1..5, sidebar-* */
}

.dark {
    --background: oklch(0.145 0 0);
    --foreground: oklch(0.985 0 0);
    --primary: oklch(0.922 0 0);
    --primary-foreground: oklch(0.205 0 0);
    --destructive: oklch(0.704 0.191 22.216);
    --border: oklch(1 0 0 / 10%);
    --input: oklch(1 0 0 / 15%);
    --ring: oklch(0.556 0 0);
    /* dark values for the same set */
}

@layer base {
  * {
    @apply border-border outline-ring/50;
    }
  body {
    @apply bg-background text-foreground;
    }
  html {
    @apply font-sans;
    }
}
```

The exact generated file is 200+ lines and is produced by the CLI. Do not hand write
it; run `shadcn init` and then edit only the `:root` and `.dark` values if the brand
palette changes. `baseColor` (neutral here) cannot be changed after init.

### frontend/components.json (written by `shadcn init`)

```json
{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "radix-nova",
  "rsc": false,
  "tsx": true,
  "tailwind": {
    "config": "",
    "css": "src/index.css",
    "baseColor": "neutral",
    "cssVariables": true,
    "prefix": ""
  },
  "iconLibrary": "lucide",
  "rtl": false,
  "aliases": {
    "components": "@/components",
    "utils": "@/lib/utils",
    "ui": "@/components/ui",
    "lib": "@/lib",
    "hooks": "@/hooks"
  },
  "menuColor": "default",
  "menuAccent": "subtle",
  "registries": {}
}
```

`tailwind.config` is deliberately the empty string on Tailwind 4. `rsc` is false for
Vite.

### frontend/src/lib/utils.ts

```ts
export { cn } from "cn"
```

That is the whole file in shadcn 4. The `clsx` plus `tailwind-merge` implementation is
gone, replaced by the compiled `cn` package (0.2.6), described by its author as a
drop-in replacement for clsx plus tailwind-merge. Generated components import
`{ cn } from "cn"` directly, not from `@/lib/utils`.

## 5. What changed in shadcn 4 that older guides get wrong

This is the single largest source of stale advice. Anything written before roughly
mid 2026 describes shadcn 2.x or 3.x.

1. `shadcn` is now a runtime dependency, not only a CLI. `package.json` gets
   `"shadcn": "^4.21.0"` and the CSS does `@import "shadcn/tailwind.css"`. That sheet
   (629 lines) supplies the accordion keyframes and a large set of `@custom-variant`
   helpers: `data-open`, `data-closed`, `data-checked`, `data-unchecked`,
   `data-selected`, `data-disabled`, `data-active`, `data-horizontal` and more.
2. Three component bases exist: `base` (Base UI), `radix` (Radix UI), `aria`
   (React Aria). Chosen with `-b` at init. The registry index carries per base doc and
   API links for every item. Recommendation for this app: `radix`, because it is the
   most battle tested and the one nearly all community examples target. Note that even
   on the radix base, `combobox` and `calendar` pull `@base-ui/react` and
   `react-day-picker`.
3. Styles are now base plus preset, for example `radix-nova`. `new-york` and `default`
   still appear inside the CLI bundle as legacy base-colour identifiers.
4. There is no longer a real `form` component. `https://ui.shadcn.com/r/index.json`
   still lists `form`, but `https://ui.shadcn.com/r/styles/radix-nova/form.json`
   returns an item with an empty `files` array, so `shadcn add form` writes nothing and
   installs no `react-hook-form`. Forms are built from `field` plus your own form
   library. The docs name React Hook Form, TanStack Form or Formisch.
5. `radix-ui` is installed as one package, not `@radix-ui/react-dialog` and friends.
6. `sonner` is the toast. The generated `sonner.tsx` calls `useTheme()` from
   `next-themes`, so `next-themes` lands in the dependency tree automatically.
7. The registry currently exposes 63 items:
   accordion, alert, alert-dialog, aspect-ratio, attachment, avatar, badge, breadcrumb,
   bubble, button, button-group, calendar, card, carousel, chart, checkbox, collapsible,
   combobox, command, context-menu, dialog, direction, drawer, dropdown-menu, empty,
   field, form, hover-card, input, input-group, input-otp, item, kbd, marker, menubar,
   message, message-scroller, native-select, navigation-menu, pagination, popover,
   progress, questionnaire, radio-group, resizable, scroll-area, select, separator,
   sheet, sidebar, skeleton, slider, sonner, spinner, switch, table, tabs, textarea,
   toast, toggle, toggle-group, tooltip.

## 6. Recommended component set for ExpiryManager screens

Verified installable in one command; all 38 landed in `src/components/ui/`.

- Shell and navigation: `sidebar`, `breadcrumb`, `tabs`, `separator`, `scroll-area`.
- Underlyings and expiries catalogue: `table`, `badge`, `checkbox`, `input`,
  `input-group`, `pagination`, `dropdown-menu`, `select`, `combobox`, `command`.
- Expiry multi-select: there is no `multi-select` primitive. Build it as
  `popover` + `command` + `checkbox` (the command-list checkbox pattern) for a
  searchable list, or use the data table's own row selection when the expiry list is
  already the table. For 300-plus expiries per underlying, prefer the table with row
  selection plus a "select all filtered" action, because it reuses the same filtering
  and keeps one mental model.
- Credentials and job forms: `field` (FieldSet, FieldLegend, FieldGroup, Field,
  FieldLabel, FieldContent, FieldDescription, FieldError, FieldSeparator), `input`,
  `label`, `switch`, `radio-group`, `textarea`, `calendar` for date ranges.
- Dialogs and confirmation: `dialog`, `alert-dialog`, `sheet` (side panel for job
  detail), `tooltip`, `popover`.
- Feedback: `sonner` (toasts), `progress`, `spinner`, `skeleton`, `alert`, `empty`
  (empty states for "no expiries downloaded yet"), `item`, `kbd`.
- Layout: `card`, `button`, `toggle-group` (resolution switcher: 1m, 5m, 15m, 60m).

Deliberately not used: the shadcn `chart` component. It wraps Recharts, and this app
charts through openalgo-charts.

## 7. Data table: TanStack Table v9

This is a breaking rewrite, released 2026-08-04, and it is what `@tanstack/react-table`
`latest` now resolves to. Almost every data-table example on the internet is v8.

v8 to v9 differences that bite:

- `useReactTable` is gone. The hook is `useTable`.
- Features are opt in, registered as plugins. Nothing is included unless asked for, so
  unused feature code tree shakes away.
- Row model factories are registered inside `tableFeatures(...)`, not passed as
  `getSortedRowModel: getSortedRowModel()` options.
- `flexRender(...)` as a call is replaced by the components `table.FlexRender`
  (`<table.FlexRender header={header} />` and `<table.FlexRender cell={cell} />`).
  A standalone `flexRender` export still exists for compatibility.
- Row cells come from `row.getAllCells()`.
- `useLegacyTable` exists as a temporary v8-shaped escape hatch.
- `createSortedRowModel()`, `createFilteredRowModel()` and `createPaginatedRowModel()`
  take zero arguments in 9.2.4. Passing `sortFns` or `filterFns` is a type error
  (`TS2554: Expected 0 arguments, but got 1`).
- `ColumnDef` is `ColumnDef<TFeatures, TData, TValue = unknown>`. The features object
  type is the first parameter now. Do not pin `TValue` to `string` on a column list
  that is typed as a whole array, the array becomes unassignable.

Minimal shape that compiled cleanly in the test build:

```tsx
import {
  columnFilteringFeature,
  createFilteredRowModel,
  createPaginatedRowModel,
  createSortedRowModel,
  rowPaginationFeature,
  rowSelectionFeature,
  rowSortingFeature,
  tableFeatures,
  useTable,
} from '@tanstack/react-table'
import type { ColumnDef } from '@tanstack/react-table'

const features = tableFeatures({
  rowSortingFeature,
  columnFilteringFeature,
  rowPaginationFeature,
  rowSelectionFeature,
  sortedRowModel: createSortedRowModel(),
  filteredRowModel: createFilteredRowModel(),
  paginatedRowModel: createPaginatedRowModel(),
})

const columns: ColumnDef<typeof features, Expiry>[] = [
  { id: 'expiry', accessorKey: 'expiry', header: 'Expiry' },
]

const table = useTable({
  features,
  columns,
  data,
  getRowId: (row) => row.expiry,
})

// render
table.getHeaderGroups().map((hg) => hg.headers.map((h) => <table.FlexRender header={h} />))
table.getRowModel().rows.map((row) => row.getAllCells().map((c) => <table.FlexRender cell={c} />))
table.nextPage()
```

Other v9 exports worth knowing: `createColumnHelper`, `globalFilteringFeature`,
`columnVisibilityFeature`, `columnPinningFeature`, `columnResizingFeature`,
`rowExpandingFeature`, `columnFacetingFeature` with `createFacetedUniqueValues` and
`createFacetedMinMaxValues`, plus `table.Subscribe` and an `atoms` option for
fine grained subscriptions on very large tables.

Decision for ExpiryManager: for the expired-contract browser, which can be tens of
thousands of rows, do server side pagination, sorting and filtering against FastAPI
(and therefore DuckDB) and keep the table in manual mode, rather than registering
`paginatedRowModel` and shipping every row to the browser. Register only
`rowSortingFeature` and `rowSelectionFeature` client side, drive `sorting` and
`columnFilters` state into the query key, and let the server return the page.
UNVERIFIED: the exact v9 option name for manual pagination (v8 called it
`manualPagination`); check `TableOptions_RowPagination` in the installed types before
writing that code.

If a stable, well documented data table matters more than being on latest, pinning
`@tanstack/react-table@^8` is a legitimate fallback; the v8 API is what the shadcn
data-table guides used until recently. The shadcn docs themselves have already moved to
v9.

## 8. Client data layer: TanStack Query v5

`@tanstack/react-query` latest is 5.102.8. There is no v6. Peer is `react: ^18 || ^19`.
Defaults from the official "Important Defaults" page: queries are stale immediately
(`staleTime: 0`), inactive queries are garbage collected after 5 minutes
(`gcTime: 1000 * 60 * 5`), failed queries retry 3 times with exponential backoff.

Object-syntax API, and the function form of `refetchInterval` receives the query, which
is what makes job polling clean. This compiled and built:

```tsx
const job = useQuery({
  queryKey: ['job', jobId],
  queryFn: () => api.getJob(jobId),
  // Stop the timer as soon as the pipeline run reaches a terminal state.
  refetchInterval: (query) =>
    query.state.data?.status === 'running' ? 1000 : false,
})
```

Recommended shape for this app:

- One `QueryClient` at the root with `defaultOptions.queries.staleTime` around 30
  seconds for catalogue data (underlyings, expiry lists) and 0 for job state.
- Query keys: `['underlyings']`, `['expiries', underlying]`,
  `['contracts', underlying, expiry]`, `['jobs']`, `['job', jobId]`,
  `['ohlcv', symbol, resolution, from, to]`.
- Mutations for start job, cancel job, save credentials, add underlying, each followed
  by `queryClient.invalidateQueries({ queryKey: ['jobs'] })`.
- `@tanstack/react-query-devtools` in dev only.
- For the very large OHLCV pulls that feed openalgo-charts, do not put raw bar arrays
  in the query cache without a `gcTime` cap; charts want the array once, and holding
  several symbols of minute data will grow the heap.

### Polling versus SSE versus WebSocket for pipeline progress

Recommendation: Server Sent Events for pipeline progress, with polling as the fallback,
and no WebSocket unless a genuinely bidirectional need appears later.

Reasons, in order:

1. Progress is one directional: server to browser. SSE is exactly that shape.
2. SSE is plain HTTP, so it inherits the cookie auth and the CSRF posture already in
   place. A WebSocket upgrade sends cookies but is not covered by CORS, so it needs its
   own origin check on the FastAPI side.
3. SSE reconnects automatically with `Last-Event-ID`, which suits a downloader that may
   run for an hour across dozens of expiries.
4. It was verified to pass through the Vite 8 dev proxy unbuffered. Against a local
   Python SSE endpoint proxied through `/api`, `curl -N http://localhost:5199/api/stream`
   returned five `event: progress` frames one at a time, in real time, not batched at
   the end.

Server side: `sse-starlette` 3.4.11 (PyPI, requires Python >= 3.10) with FastAPI
0.141.1 and uvicorn 0.52.4. Send `Cache-Control: no-cache` and
`X-Accel-Buffering: no` so no intermediate proxy buffers the stream.

Client side integration with TanStack Query: do not try to make `useQuery` own the
stream. Open the `EventSource` in an effect and push each frame into the cache:

```tsx
useEffect(() => {
  const es = new EventSource('/api/jobs/stream')
  es.addEventListener('progress', (e) => {
    const p = JSON.parse((e as MessageEvent).data)
    queryClient.setQueryData(['job', p.jobId], p)
  })
  es.addEventListener('done', () => queryClient.invalidateQueries({ queryKey: ['jobs'] }))
  return () => es.close()
}, [queryClient])
```

Fallback: keep the `refetchInterval` polling query above, and disable it while the
`EventSource` `readyState` is OPEN. That keeps the UI correct if a corporate proxy or
an antivirus TLS shim breaks streaming.

Caveat on the native `EventSource`: it cannot send custom headers. This is fine because
auth is a cookie, and the cookie rides along on same-origin requests. If a bearer token
is ever needed, swap to `@microsoft/fetch-event-source` 2.0.1 (UNVERIFIED whether that
package is still maintained; check before adopting).

WebSocket is still worth having plumbed in the proxy (`'/ws': { target: 'ws://...',
ws: true }`) because the openalgo-charts live tick path may want it in Phase 2. Vite's
docs warn against `rewriteWsOrigin`, since Vite does not check the origin of WebSocket
requests before proxying and rewriting the origin opens a CSRF hole. Leave it off.

## 9. Dark mode with Tailwind 4

Tailwind 4's `dark` variant is `prefers-color-scheme` by default. Class based toggling
requires overriding the variant in CSS. `shadcn init` already wrote the override into
`src/index.css`:

```css
@custom-variant dark (&:is(.dark *));
```

The Tailwind docs give the equivalent as `@custom-variant dark (&:where(.dark, .dark *))`,
and the data-attribute form as
`@custom-variant dark (&:where([data-theme=dark], [data-theme=dark] *))`. Keep the
shadcn-generated line; do not add a second one.

Two workable providers:

1. `next-themes` 0.4.6, which is already in the tree because `sonner.tsx` calls its
   `useTheme`. It is framework agnostic. Wrap the app in
   `<ThemeProvider attribute="class" defaultTheme="system" enableSystem storageKey="expirymanager-theme">`.
   This is the lower friction option precisely because the generated sonner component
   expects it.
2. The hand rolled shadcn `theme-provider.tsx` from the Vite dark-mode doc, which does
   the same thing with a React context, `localStorage` and a `prefers-color-scheme`
   media query. Choose this only if `next-themes` is dropped, and then patch
   `sonner.tsx` to use your own `useTheme`.

To avoid a flash of the wrong theme, put the toggle inline in `index.html` head, per the
Tailwind docs:

```js
document.documentElement.classList.toggle(
  "dark",
  localStorage.theme === "dark" ||
    (!("theme" in localStorage) && window.matchMedia("(prefers-color-scheme: dark)").matches),
);
```

### Keeping openalgo-charts in step

openalgo-charts 2.1.0 exports `darkTheme`, `lightTheme`, `DEFAULT_THEME` and the type
`ChartTheme` from its root entry, and the widget tier exposes `widget.setTheme('light')`
or `setTheme(themeObject)`. Per its docs, the widget derives every chrome colour from
the active `ChartTheme` and writes them as `--oac-` custom properties on the widget
root, so `setTheme` recolours canvas and chrome together without a manual repaint.

Wiring: subscribe to the app theme once and call `setTheme` on every chart or widget
instance.

```tsx
const { resolvedTheme } = useTheme()      // next-themes
useEffect(() => {
  widgetRef.current?.setTheme(resolvedTheme === 'dark' ? 'dark' : 'light')
}, [resolvedTheme])
```

If a fully brand-matched chart is wanted, build a `ChartTheme` object from the shadcn
oklch tokens by reading them off `document.documentElement` with
`getComputedStyle(...).getPropertyValue('--background')`. Note the tokens are oklch
strings; openalgo-charts expects colour strings, and canvas accepts oklch in current
browsers. UNVERIFIED: whether openalgo-charts parses oklch anywhere itself rather than
handing the string to the canvas context. Test before relying on it, and fall back to
hex if it does not.

One gotcha: the local checkout at
`/Users/openalgo/AIBootcamp2026/Day26/openalgo-charts` has no `dist/` directory, so a
`file:` dependency will not resolve until `npm run build` (rollup) is run there.
Version 2.1.0 is published on npm, so `npm install openalgo-charts` is the simpler path
unless local chart changes are being made in parallel.

## 10. Dev server proxy, cookies and CSRF

Verified behaviour of the Vite 8 dev proxy (it uses `http-proxy-3` internally):

- `'/api': { target: 'http://127.0.0.1:8000', changeOrigin: true }` forwards the path
  unchanged, so FastAPI should mount its routes under `/api`. No `rewrite` needed. Use
  `rewrite: (p) => p.replace(/^\/api/, '')` only if FastAPI serves at the root.
- `Set-Cookie` passes through untouched. A curl through the proxy returned
  `set-cookie: session=abc; Path=/; HttpOnly; SameSite=Lax` verbatim. Because the
  browser only ever sees `http://localhost:5173`, the cookie is first party and
  `SameSite=Lax` is the correct setting. Do not set `Domain` on the cookie, and do not
  set `Secure` in dev (it would be dropped over plain http on localhost in some
  browsers).
- Vite adds `Vary: Origin` to proxied responses; harmless.
- A key starting with `^` is treated as a RegExp.
- Non-relative `base` requires prefixing every proxy key with that base.

CSRF posture that fits this setup:

- Session cookie: `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` in production.
- Double-submit CSRF token: a second, non-HttpOnly cookie (`csrf_token`) that the
  frontend reads and echoes as an `X-CSRF-Token` header on every unsafe method. Add it
  once in the fetch wrapper that TanStack Query calls, not per mutation.
- FastAPI must also check `Origin` on unsafe methods, since `SameSite=Lax` alone does
  not cover every case and the dev proxy makes the request look same-origin anyway.
- In production, serve the built `dist/` from FastAPI itself (StaticFiles with an
  SPA fallback) so there is exactly one origin and CORS is never needed at all. That
  matches the zero-config, single-process goal. Only if a separate frontend host is
  ever used does `CORSMiddleware` with `allow_credentials=True` and an explicit origin
  list become necessary.
- `server.allowedHosts` and `server.cors` exist on Vite's dev server for host-header
  and CORS hardening; leave the defaults unless the dev server is exposed with `--host`.

## 11. Suggested frontend layout

```
frontend/
  index.html
  vite.config.ts
  tsconfig.json  tsconfig.app.json  tsconfig.node.json
  components.json
  src/
    main.tsx
    App.tsx
    index.css
    lib/
      utils.ts            # export { cn } from "cn"
      api.ts              # fetch wrapper, CSRF header, error envelope
      query-client.ts
    components/
      ui/                 # shadcn generated, do not hand edit casually
      layout/             # AppSidebar, Header, ThemeToggle
      catalog/            # UnderlyingPicker, ExpiryTable, AddUnderlyingDialog
      jobs/               # JobList, JobDetailSheet, JobProgress, useJobStream
      charts/             # OpenAlgoChart wrapper, theme bridge
    hooks/
    routes/               # if react-router 7.18.3 is added
    types/
```

## 12. Open items for the design phase

- Router: nothing was installed. `react-router-dom` latest is 7.18.3. TanStack Router
  is the other candidate and pairs naturally with TanStack Query, but it was not
  version checked here. A four or five screen app could also skip a router and use
  `tabs`.
- Whether to pin `@tanstack/react-table` to v8 for the maturity of the ecosystem, or
  take v9 as verified above.
- `oxlint` (what create-vite now ships) versus ESLint 10.10.0. oxlint is faster and is
  the scaffold default; ESLint has the wider plugin set. openalgo-charts itself uses
  ESLint with a tier ACL, so consistency across the two repos may matter.
- Manual pagination option names in TanStack Table v9.
- Whether openalgo-charts accepts oklch colour strings in a custom `ChartTheme`.
