// Runs before first paint to stop a light flash on a dark install.
//
// This lives in a file rather than inline in index.html on purpose: the production CSP is
// script-src 'self', so an inline block would be blocked once FastAPI serves dist/. It is a
// classic (non-module, non-defer) script so the browser executes it before the body renders.
//
// The storage key and value vocabulary are next-themes': the raw stored value is one of
// "light", "dark" or "system", and anything unrecognised is treated as "system".
(function () {
  try {
    var stored = window.localStorage.getItem('expirymanager-theme');
    var dark =
      stored === 'dark' ||
      (stored !== 'light' &&
        window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.classList.toggle('dark', dark);
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
  } catch {
    // Storage can throw in a locked-down browser profile. The provider will settle the class
    // a moment later, so a flash is the whole cost of this failing.
  }
})();
