# openalgo-charts integration for ExpiryManager

Research notes. Source of truth is the local checkout at
`/Users/openalgo/AIBootcamp2026/Day26/openalgo-charts`, read directly (not from memory).

## 0. Provenance and version

| Fact | Value | Where verified |
| --- | --- | --- |
| Package name | `openalgo-charts` | `package.json` |
| Local checkout version | `2.1.0` | `package.json`, `src/version.ts` (both agree) |
| npm registry latest | `2.1.0` | `npm view openalgo-charts version` |
| Local git position | `v2.1.0-4-g287627b`, working tree clean | `git describe --tags`, `git status --porcelain` |
| The 4 commits past the tag | docs and demo-page fixes only, no `src/` library change of substance | `git log --oneline -3` |
| Module system | ESM only. No `main`, no `require` condition, no CJS build | `package.json` `exports` |
| Runtime dependencies | none. `dependencies` is absent; everything is `devDependencies` build tooling | `package.json` |
| Engines | `node >= 20` (we have 26.4.0) | `package.json` |
| CSS to import | none. The widget injects its own single `<style>` element | `src/widget/styles.ts` |
| `dist/` present in the local checkout | NO. It is gitignored and must be built | `ls dist` fails |

The published 2.1.0 and the local `src/` are the same library. That single fact drives the
install recommendation in section 2.

## 1. The DataFeed contract (exact, from `src/feed/types.ts` and `src/model/bar.ts`)

This is the entire surface an ExpiryManager adapter has to satisfy. Nothing else.

```ts
// src/model/bar.ts
/** Internal time is always UTC seconds (integer). */
export type UTCSeconds = number;

export interface Bar {
  time: UTCSeconds;   // integer UTC seconds, NOT milliseconds, NOT a string
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;    // optional
  color?: string;     // optional per-bar colour override
}

// src/feed/types.ts
export type UnsubscribeFn = () => void;

export interface BarsRequest {
  symbol: string;
  exchange: string;
  interval: string;      // an interval code, e.g. "1m", "5m", "1h", "D"
  from?: UTCSeconds;     // optional on the interface; the widget always sends it
  to?: UTCSeconds;       // optional on the interface; the widget always sends it
}

export interface DataFeed {
  getBars(req: BarsRequest): Promise<Bar[]>;
  subscribeBars?(req: BarsRequest, onBar: (bar: Bar) => void): UnsubscribeFn;
  subscribeDepth?(
    req: BarsRequest,
    onDepth: (depth: MarketDepth) => void,
    opts?: { depthLevel?: number },
  ): UnsubscribeFn;
}
```

Required: `getBars` only. `subscribeBars` and `subscribeDepth` are optional and must be
**omitted entirely** rather than stubbed with a no-op, because callers feature-detect them
(`if (feed.subscribeBars)`). The shipped `OpenAlgoDataFeed` is history-only and deliberately
omits `subscribeBars`; that is exactly ExpiryManager's shape for expired contracts.

Rules for any adapter, quoted from `references/feeds-and-live.md`:

- convert to UTC seconds at the edge,
- sort ascending,
- keep one bar per time,
- omit the optional methods rather than shipping a no-op.

Every type above is exported from the base package, so ExpiryManager imports them as types
only:

```ts
import type { Bar, BarsRequest, DataFeed, UnsubscribeFn, UTCSeconds } from 'openalgo-charts';
```

Verified in `src/index.ts` lines 213 to 218.

### Timestamps line up with Fyers with zero conversion

Fyers expired historical-data returns candles as
`[timestamp, open, high, low, close, volume, open_interest?]` where `timestamp` is epoch
**seconds** (sample row from the docs: `[1742967900, 680.50, 708.85, 608.00, 640.00, 146025, 4987175]`,
which is 2025-03-26 05:45 UTC, 11:15 IST). `Bar.time` is UTC seconds. So the Fyers epoch value
passes straight through to `Bar.time` with no arithmetic anywhere in the pipeline, provided
the FastAPI layer and DuckDB also store UTC seconds (or a UTC-aware timestamp we render back
to epoch seconds). Recommend storing the raw epoch integer as the canonical bar time column.

### Interval codes

`src/feed/intervals.ts`. Built-in token grammar is an optional count plus one of `s m h d w`,
case-insensitive except for `M`:

- `1m`, `5m`, `15m`, `45m`, `1h`, `2h`, `4h`, `1d`, `D`, `1w`, `W`, `5s` all resolve.
- Upper-case `M` deliberately does **not** resolve to minutes (a month is not 60 seconds); it
  must be registered as a calendar interval if we ever want monthly bars.
- An unrecognised code throws `UnknownIntervalError` rather than silently becoming 60 seconds.
  Validate a picker's input with `isKnownInterval(code)`.

Every Fyers minute resolution ExpiryManager needs maps to a built-in token:

| Fyers `resolution` | openalgo-charts interval code |
| --- | --- |
| `1`, `2`, `3`, `5`, `10`, `15`, `20`, `30`, `45` | `1m`, `2m`, `3m`, `5m`, `10m`, `15m`, `20m`, `30m`, `45m` |
| `60`, `120`, `180`, `240` | `1h`, `2h`, `3h`, `4h` |
| `5S`, `10S`, `30S` | `5s`, `10s`, `30s` |

So the ExpiryManager adapter needs one small map from the chart's interval code to the Fyers
`resolution` string, and nothing else. Keep that map on the backend if the backend is the one
talking to Fyers; the frontend adapter then just forwards the code.

## 2. Installing a local checkout into a Vite 8 plus React app

Four concrete options.

**Option A: install from npm at the matching version. RECOMMENDED.**

```bash
npm install openalgo-charts@2.1.0
```

Reasons it wins here:

- The registry's 2.1.0 is byte-identical in library terms to this checkout (same version
  string in `package.json` and `src/version.ts`, only doc and demo commits after the tag).
- It ships a prebuilt, minified, tier-split `dist/` with `.d.ts` per tier. The local checkout
  has no `dist/` at all, and `package.json` `files` publishes only `dist/**`, so every other
  option requires a build step first.
- Vite resolves the `exports` map cleanly, dependency optimisation works, and the ESLint tier
  ACL and registry-identity guarantees hold without any alias configuration.
- Zero runtime dependencies, so `npm audit` and lockfile churn are nil.
- Pin the exact version (no caret) so a chart-breaking minor cannot arrive unnoticed.

**Option B: `file:` dependency on the checkout.**

```json
{ "dependencies": { "openalgo-charts": "file:../openalgo-charts" } }
```

Works, but only after `cd ../openalgo-charts && npm install && npm run build`, because
`exports` points at `./dist/openalgo-charts.mjs`. npm copies (or symlinks, depending on
version) the package, so a rebuild of the library needs a reinstall to be picked up. Choose
this only if we intend to patch the charting library alongside ExpiryManager.

**Option C: `npm link`.**

Same build prerequisite as B, plus the classic Vite symlink hazards: `preserveSymlinks`
behaviour, duplicate module instances if anything resolves through two paths, and
`optimizeDeps` caching a stale copy. If used, add
`resolve: { preserveSymlinks: false }` and `optimizeDeps: { exclude: ['openalgo-charts'] }`
and expect to clear `node_modules/.vite` after library rebuilds. Not worth it here.

**Option D: build `dist/` and copy or serve it, importing concrete `.mjs` paths.**

Documented and supported (the tier bundles import siblings relatively, so `dist/` served as-is
needs no import map), but it forfeits TypeScript types through `exports` and hand-writes paths
that are easy to get wrong. Reserve it for a no-bundler demo page.

**Hard rule regardless of option: never deep-import into `dist/`.** Not
`openalgo-charts/dist/openalgo-charts.mjs`, not a relative path into `node_modules`, not a
tier's internal module. Every registry (chart types, indicators, drawing tools) is a
module-level `Map` in exactly one module instance; a deep import creates a second, empty one
and `chart.addIndicator('macd')` throws on a page that plainly imported the indicators tier.
Bare specifier or a declared subpath only.

## 3. Which tiers to import, and what they cost

Budgets from `.size-limit.json`, Brotli, enforced by `npm run size` in the library repo.
Measure before quoting in ExpiryManager's own docs; these are the 2.1.0 figures.

| Specifier | Contents | Budget | Import has side effects |
| --- | --- | --- | --- |
| `openalgo-charts` | engine, 13 chart types, registries, feeds, timezone, settings schema | 67 KB | no |
| `openalgo-charts/indicators` | 102 built-in indicators plus the Tier-2 contract | 30 KB | yes, registers all 102 |
| `openalgo-charts/draw` | 51 drawing tools, `DrawingController` | 26 KB | yes |
| `openalgo-charts/widget` | `createWidget` plus all chrome, the only tier that ships DOM. Imports `openalgo-charts/draw` itself | 36 KB | yes, registers 7 dialogs |
| `openalgo-charts/transform` | Renko, Range, P&F, Kagi, Line Break, Heikin Ashi | 5 KB | yes, registers 2 chart types |
| `openalgo-charts/profile` | Volume Profile, TPO, Footprint, orderflow | 11 KB | no |
| `openalgo-charts/webgl` | WebGL2 series backend | 7 KB | yes |
| `openalgo-charts/trade` | order lines, DOM ladder, `OrderEngine` | (base + trade 75 KB) | no |

**For ExpiryManager's candles plus indicators, the minimum import set is:**

```ts
import { createWidget } from 'openalgo-charts/widget';  // pulls the base and the draw tier
import 'openalgo-charts/indicators';                    // otherwise the picker is empty
```

That is the `Widget terminal` size row: base plus draw plus indicators plus widget, budget
156 KB Brotli (measured 155.09 KB at 2.1.0). If we want candles and indicators but write our
own chrome, drop the widget and the draw tier and it is base plus indicators, roughly 97 KB
budget. Given ExpiryManager is a research tool, not a latency-critical trading front end,
the full terminal at ~155 KB Brotli is the right trade: it is one HTTP payload, cached, and
it saves building a toolbar, a drawing rail, dialogs and a keymap.

Tiers we should **not** import for the first release: `trade` (no order entry in a research
tool), `webgl` (only worth it at very large bar counts, and it is a render backend swap we
can add later without touching the feed), `profile` and `transform` (add them the day a
research question needs footprint or Renko; both are lazy `await import()` candidates).

Lazy loading a tier the user may never touch is supported:

```ts
const { createTier2Indicator } = await import('openalgo-charts/indicators');
```

`sideEffects` in `package.json` is an array, not `false`, so a bare
`import 'openalgo-charts/indicators'` is never tree-shaken away. If some future bundler
config eliminates it anyway, the registrars are exported and idempotent:
`registerBuiltinIndicators()`, `registerBuiltinDrawingTools()`, `registerTransformChartTypes()`.

## 4. `createWidget`: the full API

```ts
function createWidget(container: HTMLElement | string, options: WidgetOptions = {}): Widget;
```

`container` is an `HTMLElement`, a CSS selector, or an element id (resolved with
`querySelector` then `getElementById`). It must have a non-zero size before the call. The
widget **appends** a `.oac-widget` root to the container rather than replacing its content,
and `destroy()` removes that root again.

Nothing in `WidgetOptions` is required. `createWidget(el)` alone is a full terminal with no
data. In practice `feed`, `symbol`, `interval` and `theme` are what ExpiryManager sets.

### `WidgetOptions` (verified against `src/widget/widget.ts` lines 55 to 93)

`WidgetOptions extends Omit<ChartOptions, 'theme'>`, so every `ChartOptions` key
(`timezone`, `crosshairMode`, `axisChrome`, `legendOffset`, `zoomAnchor`, `animZoom`,
`priceAxisWidth`, `timeAxisHeight`, `ariaLabel`, `document`, `pixelRatio`, `raf` and the
rest) passes straight through to `createChart`, minus the widget-only keys.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `feed` | `DataFeed` | none | `getBars` on start and on every `setSymbol` / `setInterval` / `reload`; `subscribeBars` only when the feed has it. |
| `symbol` | `string` | `''` or the saved one | Upper-cased on the way in and by `setSymbol`. |
| `exchange` | `string` | `''` | Passed to the feed with the symbol. |
| `interval` | `string` | `'1d'` or the saved one | Must resolve, else `UnknownIntervalError` at the call site. |
| `intervals` | `readonly string[]` | `DEFAULT_INTERVALS` (`1m 5m 15m 1h 1d 1w`) plus every registered code | The pill list. Each is validated. |
| `chartType` | `string` | `'candlestick'` | A registered chart type id, else it throws. |
| `theme` | `'dark'` or `'light'` or `ChartTheme` | `'dark'` | Drives the canvas and the chrome tokens. Note the engine's own default is light, the widget's is dark. |
| `rail` | `boolean` or `RailOptions` | on | `false` hides the drawing rail. `tools` restricts ids, `favorites` seeds pins. |
| `topbar` | `boolean` | on | |
| `statusline` | `boolean` | on | |
| `indicators` | `boolean` | on | The Indicators button and picker. |
| `persist` | `boolean` or `string` | off | `true` uses namespace `default`; a string names one. Key: `oac-widget:<ns>:state`. |
| `storage` | `StorageLike` or `null` | page `localStorage` | The store behind `persist`. |
| `locale` | `string` | runtime default | BCP 47 tag for status-line numbers. |
| `symbolSearch` | `(query: string) => SymbolMatch[] \| Promise<SymbolMatch[]>` | none | Runs after 150 ms of quiet. `SymbolMatch` is `{ symbol, exchange?, name? }`. |
| `lookbackBars` | `number` | `500` | Bars per load. See the load-window trap in section 5. |
| `now` | `() => number` | `Date.now` | Clock for the load window and the capture filename. |
| `onOrder` | `(order: OrderRequest) => void` | none | Right-click order entry. Leave unset for ExpiryManager: without it the menu draws no trade rows. |

`now` is consumed by the widget and stripped from what reaches `createChart`
(`WIDGET_ONLY_KEYS` in `src/widget/widget.ts`), so passing it does not disturb the chart's
own kinetic-animation clock. It also means a host cannot set `ChartOptions.now` through the
widget; that is fine, we do not want to.

### The `Widget` handle

```ts
widget.chart;        // Chart, the full base API
widget.draw;         // DrawingController
widget.series;       // the primary SeriesApi (replaced by setChartType)
widget.root;         // the .oac-widget element
widget.context;      // WidgetContext, what every dialog was handed
widget.symbol(); widget.exchange(); widget.interval(); widget.chartType(); widget.theme();
widget.setSymbol(symbol, exchange?);   // upper-cases, triggers reload when a feed is set
widget.setInterval(code);              // throws UnknownIntervalError for an unknown code
widget.setChartType(id);               // rebuilds the series with the same bars
widget.setTheme('dark' | 'light' | chartTheme);
widget.openSettings();                 // false when no settings dialog is registered
widget.openIndicatorPicker();
await widget.reload();                 // refetch for the current symbol and interval
widget.getState();                     // JSON-safe WidgetState
widget.restoreState(saved);            // { applied, reason?, chart? }
widget.on(event, cb);                  // returns the unsubscriber
widget.off(event, cb?);
widget.destroy();                      // saves if persisting, removes the chrome, destroys the chart
widget.isDestroyed;
```

Behaviour worth knowing, read from the source:

- `setSymbol` no-ops (and just refreshes the top bar) when the symbol and exchange are
  unchanged. `setInterval` no-ops on the same code. So driving them from a React effect on
  every render is harmless but pointless.
- Both setters reset `_keepView`, then call `reload()`, which does `series.setData(bars)`
  followed by `chart.fitContent()`. The viewport therefore always fits the returned bars
  after a symbol or interval change. This is what makes the expired-contract case work even
  when the requested window is nowhere near the data (see section 5).
- `reload()` guards against out-of-order responses with a monotonic sequence number, and the
  **catch path checks the sequence before it toasts**. A superseded request that rejects
  (an aborted fetch, for instance) is discarded silently. So aborting a superseded fetch
  inside our adapter is safe and will not produce a spurious error toast.
- `setChartType` preserves data by reading `series.getData()` and re-setting it on the new
  series.

### Events

```ts
widget.on('symbol',   (e) => {});  // { symbol, exchange }
widget.on('interval', (e) => {});  // { interval }
widget.on('theme',    (e) => {});  // { theme, chartTheme }
widget.on('layout',   (e) => {});  // { reason, chartType? }
widget.on('data',     (e) => {});  // { symbol, interval, bars, error? }
widget.on('status',   (e) => {});  // { text, kind }
```

`data` is the one ExpiryManager wants most: it fires with `bars` (a count) on success and
with `error` plus `bars: 0` on failure. That is how the React side learns a load finished
without polling. Chart-level events (`crosshair:move`, `contextmenu`, `lazy-load`,
`indicatorSettings`) stay on `widget.chart`; drawing events on `widget.draw`.

### Theming

The chrome carries no colour of its own. `widgetTokens(theme)` derives every chrome colour
from the active `ChartTheme`: panels are the theme `background` stepped toward white or
black, borders from `axisLine` and `paneSeparator`, text from `axisText`, accent from
`lineColor`, buy and sell from `upColor` and `downColor`. The set is written as `--oac-*`
custom properties **inline on the widget root**, and one `<style>` element per document is
injected, every rule scoped under `.oac-widget`. Nothing leaks either way.

`setTheme` rewrites the tokens with no repaint of the host's own CSS. Because the tokens are
inline declarations, a host stylesheet override needs `!important`:

```css
#expiry-chart .oac-widget {
  --oac-font: "Inter", system-ui, sans-serif !important;
  --oac-radius: 4px !important;
}
```

Override tokens, never internal class names. Token groups: surfaces (`bg`, `panel`,
`panel-2`, `elev`, `elev-2`, `elev-3`, `scrim`, `shadow`), borders (`bd`, `bd-soft`,
`bd-hover`), text (`tx`, `tx-strong`, `mut`, `faint`), accent and state (`acc`, `acc-2`,
`on-bg`, `on-bd`, `ring`, `ring-soft`, `buy`, `sell`, `amber`, `danger`), scrollbars
(`sb-thumb`, `sb-thumb-hover`), type and metrics (`font`, `mono`, `fs`, `radius`, `rail-w`,
`topbar-h`, `status-h`, `ctl-h`).

**Tailwind 4 note:** the widget's stylesheet is injected at `createWidget` time and is scoped
under `.oac-widget`, so Tailwind's preflight does not reach inside it and the widget does not
reach out. For ExpiryManager's dark and light switch, drive `widget.setTheme(...)` from the
same state that drives the Tailwind theme class, so the canvas and the app agree.

## 5. Purely historical data, and the trap that comes with it

**Yes, the widget renders history with no live subscription.** `reload()` does:

```
getBars(req) -> series.setData(bars) -> chart.fitContent() -> emit 'data'
                                     -> if (feed.subscribeBars) subscribe
```

With `subscribeBars` absent the chart simply never updates after the initial paint. No error,
no warning, no placeholder. The shipped `OpenAlgoDataFeed` works exactly this way and is the
documented history-only shape. ExpiryManager's feed should omit `subscribeBars` and
`subscribeDepth` entirely.

**THE TRAP, and it is the single most important finding for this project.** The widget
computes the request window from *now*:

```ts
// src/widget/widget.ts
export function loadWindow(interval: string, lookback: number, nowSec: number) {
  const d = tryResolveInterval(interval);
  const seconds = d !== null && d.bucketing.mode === 'interval' ? d.bucketing.seconds : null;
  const span = seconds === null ? 5 * 365 * 86400 : Math.max(1, Math.round(lookback)) * seconds;
  return { from: nowSec - span, to: nowSec };
}
```

For a 5-minute chart with the default 500-bar lookback that is a window of about 42 hours
ending now. An option contract that expired in March 2025 has **no bars in that window at
all**, so a naive adapter that honours `from` and `to` literally would return an empty array
and the terminal would sit there saying "No bars", forever, for every expired contract we
own. This is not a hypothetical: it is the default behaviour.

Three ways out, in order of preference:

1. **Clamp the window onto the contract's own traded range inside the adapter.**
   ExpiryManager's catalog already knows each expired contract's first and last traded
   timestamp (we are the ones who downloaded it). The adapter asks the backend for the
   contract's bounds once, caches them per symbol, and rewrites the request:
   `to = min(req.to, last_ts)`, `from = max(req.from - (req.to - to), first_ts)`, that is,
   slide the requested span back so it ends at the contract's last bar instead of at now.
   The widget then calls `fitContent()` on whatever comes back and everything looks right.
   This preserves the meaning of `lookbackBars` and of pan-driven paging. RECOMMENDED.

2. **Pass `now: () => contractLastTradedMs`** to `createWidget`. Clean and one line, but
   `now` is fixed at construction, so switching to a different expiry means recreating the
   widget, which defeats the whole point of `setSymbol`. Use only for a single-contract,
   single-purpose chart page.

3. **Ignore `from` and `to` in the adapter and always return the contract's full series.**
   Simplest, and fine for a single expiry of 5-minute bars (a weekly option's whole life at
   5m is a few hundred bars). It becomes wasteful at 1-minute resolution over a quarterly
   future, and it makes history paging meaningless. Acceptable as a first cut, replace with
   option 1.

**Paging older bars.** Independent of the above, wire infinite scroll on the chart handle:

```ts
let oldest = bars[0].time;
widget.chart.setHistoryLoader(async () => {
  try {
    const older = await api.barsBefore(symbol, interval, oldest, 500);
    if (older.length > 0) {
      widget.series.prependData(older);
      oldest = older[0].time;
    }
  } finally {
    widget.chart.historyLoadComplete();   // mandatory on EVERY path, including the empty one
  }
});
```

The loader fires when the visible logical range's `from` drops below 10. A latch suppresses
re-entry until `historyLoadComplete()` is called; skipping it on any path (including the
"nothing came back" path and the error path) kills paging for the rest of the session. Note
also that `prependData` shifts every logical index, so preserve the viewport explicitly if
you care:

```ts
const before = widget.chart.getVisibleLogicalRange();
widget.series.prependData(older);
widget.chart.setVisibleLogicalRange({ from: before.from + older.length, to: before.to + older.length });
```

**Bar caching.** `withBarCache(feed, opts)` wraps any `DataFeed` and gives it warm loading.
Its central rule, from the library's `CLAUDE.md`, is that closed bars are immutable and may
be cached freely while the last (forming) bar must never be served from cache. **For expired
contracts every bar is closed forever**, so ExpiryManager can cache aggressively and with a
clear conscience, which is a genuine advantage of this dataset. The wrapper keys on
`symbol|exchange|interval` and serves narrower ranges by slicing, exactly the traffic pattern
of a user flipping between expiries. Use it, but consider a long `ttlMs` and a raised `max`
for expired series, and keep the default behaviour for any live underlying chart we add later.

## 6. React integration rules (from `references/react-integration.md`)

- **Create once in a mount effect, hold in a ref, `destroy()` in the cleanup.** Never hold
  the widget or chart in `useState`: it is a mutable object graph with a running rAF loop.
- **Never construct in the component body, in `useMemo`, or before the ref is attached.**
  The constructor reads `clientWidth` / `clientHeight`, calls `getComputedStyle`, writes
  inline styles and installs a `ResizeObserver`.
- **The dep array is the whole game.** Put only genuinely identity-bearing values in the
  effect that creates the widget. An inline object or arrow in the deps rebuilds the chart on
  every parent render. Symbol, interval, theme and chart type must NOT be deps: drive them
  through `setSymbol` / `setInterval` / `setTheme` / `setChartType` from separate effects.
- **Feed instance must be stable.** Build the `ExpiryManagerDataFeed` once (module scope or a
  `useRef`), not inline in the options object.
- **Callbacks reach the instance through the ref, not a closure**, because the callback bag is
  built before the constructor returns.
- **Guard every `setState` from an async callback with a liveness flag**; the pane can unmount
  mid-fetch.
- **Do not add a window resize listener and do not pass width or height props.** The chart
  installs its own `ResizeObserver`. Size the container with CSS.
- **The container needs a resolved height.** `height: 100%` inside an auto-height parent gives
  zero pixels and nothing renders. Use an explicit height or `position: absolute; inset: 0`.
- **React Strict Mode is fine.** `destroy()` is complete and removes the root; do not try to
  memoize across the double invoke.
- **Keep orchestration out of React.** The reference production consumer puts the chart, the
  feed, the indicators and the drawing controller in a plain class with `init()` and
  `destroy()`, and lets React own only the toolbar and dialogs. For ExpiryManager, the widget
  tier already is that class, so a thin component plus imperative setters is enough.
- Anything ExpiryManager overlays on the chart as HTML must call `preventDefault` or stop
  `pointerdown`, or the chart's pointer capture eats the click.

Vite plus React needs no SSR precautions here (no Next.js, no server render), but keep the
`openalgo-charts` imports inside the chart module so a future SSR move is a one-file change.

## 7. Copy-pasteable integration sketch

### 7.1 `src/lib/charts/expiryManagerFeed.ts`

```ts
import type { Bar, BarsRequest, DataFeed } from 'openalgo-charts';

/** One row as ExpiryManager's API returns it: Fyers column order, epoch seconds first. */
type CandleRow = [number, number, number, number, number, number, number?];

interface BarsPayload {
  status: string;
  data: {
    symbol: string;
    resolution: string;
    columns: string[];
    candles: CandleRow[];
  };
}

interface ContractBounds {
  /** UTC seconds of the first and last bar we hold for this contract. */
  firstTs: number;
  lastTs: number;
}

export interface ExpiryManagerFeedConfig {
  /** Same-origin in production, the Vite proxy target in development. */
  baseUrl?: string;
  /** Injectable for tests. */
  fetchImpl?: typeof fetch;
}

/**
 * History-only feed over ExpiryManager's own FastAPI.
 *
 * `subscribeBars` and `subscribeDepth` are intentionally absent: an expired
 * contract has no live tape, and callers feature-detect the optional methods.
 */
export class ExpiryManagerDataFeed implements DataFeed {
  private readonly _baseUrl: string;
  private readonly _fetch: typeof fetch;
  private readonly _bounds = new Map<string, Promise<ContractBounds | null>>();
  private _inFlight: AbortController | null = null;

  public constructor(config: ExpiryManagerFeedConfig = {}) {
    this._baseUrl = config.baseUrl ?? '';
    // Bind to globalThis: an unbound window.fetch throws "Illegal invocation".
    const f = config.fetchImpl ?? (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : undefined);
    if (f === undefined) throw new Error('ExpiryManagerDataFeed: no fetch available');
    this._fetch = f;
  }

  public async getBars(req: BarsRequest): Promise<Bar[]> {
    const window = await this._resolveWindow(req);
    if (window === null) return [];

    // A superseded load is discarded by the widget's own sequence guard before
    // it toasts, so aborting here cannot surface a spurious error.
    this._inFlight?.abort();
    const ctl = new AbortController();
    this._inFlight = ctl;

    const url = new URL(`${this._baseUrl}/api/v1/charts/bars`, window.origin ?? location.origin);
    url.searchParams.set('symbol', req.symbol);
    url.searchParams.set('exchange', req.exchange);
    url.searchParams.set('interval', req.interval);
    url.searchParams.set('from', String(window.from));
    url.searchParams.set('to', String(window.to));

    const res = await this._fetch(url.toString(), {
      signal: ctl.signal,
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) throw new Error(`ExpiryManager: bars request failed (${res.status})`);
    const json = (await res.json()) as BarsPayload;
    return mapCandles(json.data?.candles ?? []);
  }

  /** Invalidate a contract's cached bounds, for instance after a fresh download. */
  public forget(symbol: string): void {
    this._bounds.delete(symbol);
  }

  /**
   * The widget always asks for `lookbackBars` back from now, which contains no
   * data for a contract that expired months ago. Slide the same span back so it
   * ends at the contract's last traded bar instead.
   */
  private async _resolveWindow(req: BarsRequest): Promise<{ from: number; to: number; origin?: string } | null> {
    const bounds = await this._boundsFor(req.symbol);
    if (bounds === null) return null;
    if (req.from === undefined || req.to === undefined) {
      return { from: bounds.firstTs, to: bounds.lastTs };
    }
    const span = Math.max(1, req.to - req.from);
    const to = Math.min(req.to, bounds.lastTs);
    const from = Math.max(bounds.firstTs, to - span);
    return { from, to };
  }

  private _boundsFor(symbol: string): Promise<ContractBounds | null> {
    const cached = this._bounds.get(symbol);
    if (cached !== undefined) return cached;
    const pending = this._fetchBounds(symbol);
    this._bounds.set(symbol, pending);
    return pending;
  }

  private async _fetchBounds(symbol: string): Promise<ContractBounds | null> {
    const url = `${this._baseUrl}/api/v1/charts/contracts/${encodeURIComponent(symbol)}`;
    const res = await this._fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
    if (!res.ok) return null;
    const json = (await res.json()) as { data?: { first_ts?: number; last_ts?: number } };
    const first = json.data?.first_ts;
    const last = json.data?.last_ts;
    if (typeof first !== 'number' || typeof last !== 'number') return null;
    return { firstTs: first, lastTs: last };
  }
}

/**
 * Fyers candle rows are already UTC epoch seconds, so the time column passes
 * through untouched. Open interest (column 7) has no home on `Bar`; it is
 * charted through a Tier-2 indicator instead.
 */
export function mapCandles(rows: readonly CandleRow[]): Bar[] {
  const bars: Bar[] = [];
  for (const r of rows) {
    if (!Array.isArray(r) || r.length < 6) continue;
    bars.push({
      time: Math.floor(r[0]),
      open: r[1],
      high: r[2],
      low: r[3],
      close: r[4],
      volume: r[5],
    });
  }
  bars.sort((a, b) => a.time - b.time);
  return bars;
}
```

Note the small inconsistency to fix when wiring this up for real: `_resolveWindow` returns an
optional `origin` that the sketch does not populate. If ExpiryManager is served same-origin,
build the URL with `new URL(path, location.origin)` and drop the field; if the API lives on
another origin, put its absolute base in `baseUrl` and construct `new URL(baseUrl + path)`.

### 7.2 `src/components/charts/ExpiryChart.tsx`

```tsx
import { useEffect, useRef } from 'react';
import { createWidget, type Widget } from 'openalgo-charts/widget';
import 'openalgo-charts/indicators';
import { ExpiryManagerDataFeed } from '../../lib/charts/expiryManagerFeed';

// One feed for the whole app: a new instance per render would drop the bounds cache.
const feed = new ExpiryManagerDataFeed();

const INTERVALS = ['1m', '3m', '5m', '15m', '30m', '1h'] as const;

interface Props {
  /** The expired contract, for example NSE:NIFTY25MAR23000CE. */
  symbol: string;
  exchange: string;
  interval: string;
  theme: 'dark' | 'light';
  onBarsLoaded?: (count: number, error?: string) => void;
}

export function ExpiryChart({ symbol, exchange, interval, theme, onBarsLoaded }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const widgetRef = useRef<Widget | null>(null);
  // Latest callback without making it an effect dependency.
  const loadedRef = useRef(onBarsLoaded);
  loadedRef.current = onBarsLoaded;

  // Create once. Symbol, interval and theme are driven imperatively below, so
  // they are deliberately not dependencies: they would rebuild the terminal and
  // throw away the user's indicators and drawings.
  useEffect(() => {
    const el = containerRef.current;
    if (el === null) return;
    let alive = true;

    const widget = createWidget(el, {
      feed,
      symbol,
      exchange,
      interval,
      intervals: [...INTERVALS],
      chartType: 'candlestick',
      theme,
      timezone: 'Asia/Kolkata',
      lookbackBars: 500,
      persist: 'expiry-chart',
      statusline: true,
      topbar: true,
      rail: true,
      indicators: true,
    });
    widgetRef.current = widget;

    const offData = widget.on('data', (e) => {
      if (alive) loadedRef.current?.(e.bars, e.error);
    });

    return () => {
      alive = false;
      offData();
      widget.destroy();
      widgetRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Drive the live instance instead of rebuilding it. Each setter no-ops when
  // the value is unchanged, so these effects are cheap.
  useEffect(() => { widgetRef.current?.setSymbol(symbol, exchange); }, [symbol, exchange]);
  useEffect(() => { widgetRef.current?.setInterval(interval); }, [interval]);
  useEffect(() => { widgetRef.current?.setTheme(theme); }, [theme]);

  // The container must have a resolved height or the chart gets zero pixels.
  return <div ref={containerRef} className="relative h-full w-full min-h-[480px]" />;
}
```

### 7.3 Open interest, the piece `Bar` has no field for

`Bar` carries no open-interest column, and ExpiryManager's whole point is options research,
so OI matters. The library has a first-class answer: the Tier-2 (external data) indicator
contract in `openalgo-charts/indicators`. A Tier-2 descriptor owns its own fetch and merge
lifecycle and is wrapped into an ordinary `IndicatorDescriptor`, so it appears in the
indicator picker, gets a pane, gets generated settings and is removed like any other.

```ts
import { createTier2Indicator, registerIndicator } from 'openalgo-charts/indicators';

export const OPEN_INTEREST = createTier2Indicator({
  id: 'expiry-oi',
  name: 'Open Interest',
  category: 'Options',
  placement: 'pane',
  inputs: [{ key: 'symbol', type: 'text', label: 'Contract', default: '' }],
  plots: [{ key: 'oi', type: 'line', title: 'OI' }],
  refetchOn: ['symbol'],
  fetch: async ({ settings, from, to }) => {
    const rows = await fetchOi(String(settings.symbol), from, to);
    return rows.map((r) => ({ time: r.ts, values: { oi: r.oi } }));
  },
});
registerIndicator(OPEN_INTEREST);
```

The alignment rule is last-known-value: each bar takes the most recent external point at or
before its time, never interpolated and never forward-looking, and bars before the first
point are `null`. That is the correct semantics for OI. Register the descriptor once, before
`createWidget`, so the picker lists it.

## 8. Conventions ExpiryManager's UI must honour

The library's `CLAUDE.md` writing rules apply project-wide by the user's instruction:

- **No emoji or icons anywhere**: code, comments, log messages, commit messages, docs, tests,
  terminal output. Plain text labels only. (The drawing rail's glyphs are inline SVG from the
  draw tier's icon registry, which is not an icon font and not an emoji; that is the
  sanctioned way to draw a glyph.)
- **No em dashes or en dashes anywhere.** Comma, colon, parentheses or a full stop. A plain
  hyphen inside a compound word (read-only, drop-down) is fine.
- **Comments explain why, not what**, at the density of the surrounding file.
- **Conventional Commits.**

The UI standard for host chrome, which ExpiryManager's own panels (expiry pickers, job
dashboards, scheduler screens) should clear because they sit next to the terminal:

- **Never leave a default scrollbar on a dark surface.** Style `::-webkit-scrollbar` track,
  thumb and thumb:hover, and set `scrollbar-color` plus `scrollbar-width: thin` for Firefox.
  The thumb is a step lighter than the panel, not white.
- **Colour controls are 26 to 28 px rounded squares, not full-width bars.** A 140 px colour
  block is a bug.
- **A property with a bullish and a bearish colour is one labelled row** carrying its checkbox
  and both swatches side by side, not a section header with separate Up and Down rows.
- **Prefer a dense panel that fits over a roomy one that scrolls.** Section headers small,
  uppercase, muted. Rows tight.
- **No browser-default form controls on a dark panel.** Style checkboxes and selects.
- **Tab lists carry a small glyph per tab.** Use inline SVG, not an icon font.
- **Dialog furniture**: title left, close top right, actions bottom right with the confirming
  action last, secondary control bottom left.
- **Borrow the craft, not the design.** Do not reproduce another terminal's tab taxonomy,
  panel arrangement or label phrasing. Standard domain vocabulary (logarithmic, percent,
  precision, timezone, invert) is shared property; a competitor's turn of phrase is not.
- **Never ship a control with nothing behind it.** A checkbox that does nothing is worse than
  an absent one. A control with no data in the current context is different: render it
  disabled with its state visible.
- **Trace every new option end to end**: declared, threaded, consumed, and actually changing
  output. "Declared but not consumed" is a defect, not a follow-up.

Two more that transfer directly to the data pipeline:

- **Never cache the forming bar.** Irrelevant for expired contracts (every bar is closed) but
  it becomes live the moment ExpiryManager charts a current underlying or a running future.
  The rule is not "cache less": keep the completed history and re-fetch only the tail.
- **Timezone**: IST (`Asia/Kolkata`) is the chart default and stays byte-identical for a
  caller who configures nothing. Use IANA names, never fixed offsets. Pass
  `timezone: 'Asia/Kolkata'` explicitly anyway so the intent is on the page.

## 9. Gotcha checklist

1. The widget's request window is computed from `Date.now()`. Expired contracts have no data
   in it. Clamp inside the adapter (section 5) or the chart is empty for every contract.
2. `subscribeBars` must be **omitted**, not stubbed. A no-op breaks feature detection.
3. `Bar.time` is integer UTC **seconds**. Milliseconds silently produce a chart labelled tens
   of thousands of years in the future.
4. `Bar` has no open-interest field. OI goes through a Tier-2 indicator.
5. Never deep-import into `dist/`. Two registry instances is a correctness bug that presents
   as "indicator not registered" on a page that imported the tier.
6. The indicator picker is empty unless `import 'openalgo-charts/indicators'` is present.
7. The container must have a non-zero, resolved height before `createWidget` runs.
8. `setInterval` and the `interval` / `intervals` options throw `UnknownIntervalError` for a
   code the registry does not know. Validate a picker with `isKnownInterval`.
9. Upper-case `M` does not mean minutes and does not resolve at all. Lower-case `m` is minutes.
10. The widget upper-cases symbols. Fyers symbols are already upper-case, so this is harmless,
    but do not rely on `widget.symbol()` echoing mixed case back.
11. Two widgets on one page sharing `persist: true` share a layout. Give each a namespace string.
12. `chart.historyLoadComplete()` is mandatory on every exit path of a history loader,
    including the empty and error paths, or paging dies for the session.
13. `prependData` shifts every logical index; restore the viewport explicitly.
14. Host HTML overlaid on the chart must stop `pointerdown` or the chart eats the click.
15. Host stylesheet overrides of `--oac-*` tokens need `!important` because the tokens are
    inline on the root.
16. The standalone IIFE build is base-only and cannot host the widget. Irrelevant with Vite,
    but do not reach for it as a shortcut.
17. `now` passed to `createWidget` is consumed by the widget and never reaches `createChart`.
18. `npm run size`, test counts and indicator counts must be measured, never quoted from
    memory, if ExpiryManager's docs repeat them.

## 10. Recommended shape for ExpiryManager

- Install `openalgo-charts@2.1.0` from npm, pinned.
- Import `openalgo-charts/widget` plus `openalgo-charts/indicators`. Budget ~156 KB Brotli.
- One `ExpiryManagerDataFeed` instance at module scope, wrapped in `withBarCache` with a long
  TTL for expired series.
- One `ExpiryChart` component: create in a mount effect with an empty dep array, drive symbol,
  interval and theme through the imperative setters, `destroy()` on cleanup.
- Backend contract the adapter needs: a bars endpoint returning Fyers column order with epoch
  seconds, and a contract-bounds endpoint returning `first_ts` and `last_ts`. Both same-origin,
  both `GET`, both covered by the API's rate limiting; `credentials: 'include'` so the session
  cookie rides along, and no CSRF token needed on safe methods.
- Open interest as a registered Tier-2 indicator, not as a field on `Bar`.
- Phase 2 (backtesting) note: the `openalgo-charts/trade` tier draws order lines, positions and
  a DOM ladder over a chart, and the base engine ships a `ReplayController` for market replay.
  Neither is imported now, but both are the natural surface for visualising a backtest, so do
  not architect the chart component in a way that forbids adding a tier later. Keeping the
  chart orchestration in a plain module (not spread across React state) is what preserves that.

## 11. Open questions for the design phase

1. What exactly does ExpiryManager's bars endpoint return: Fyers column order arrays (cheapest
   to produce from DuckDB and smallest on the wire) or objects? The sketch assumes arrays.
2. Where does the contract bounds (`first_ts`, `last_ts`) fact live: the SQLite catalog, or a
   DuckDB `min`/`max` per contract computed at download time? The adapter needs it to be cheap.
3. Is the frontend same-origin with the API (Vite proxy in development, FastAPI serving the
   built assets in production) or cross-origin? That decides CORS and the `baseUrl` handling.
4. Do we chart a single contract per view, or do we need multiple contracts overlaid (a strike
   ladder, a spread)? The base engine has a comparison controller and chart linking; a
   multi-contract view is a different component shape and should be decided before the first
   chart is built.
5. Should the interval pill list be static, or driven by which resolutions we have actually
   downloaded for the selected contract? The latter is better ("never ship a control with
   nothing behind it") but needs a per-contract resolution inventory from the catalog.
6. Do we want `persist` on? It writes to `localStorage` under `oac-widget:<ns>:state` and
   restores indicators and drawings across visits, which is good for research, but a saved
   viewport is dropped whenever the symbol or interval differs, so expectations must be set.
