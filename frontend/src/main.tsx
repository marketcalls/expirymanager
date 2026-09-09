import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from 'next-themes'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from '@/App'
import { Toaster } from '@/components/ui/sonner'
import '@/index.css'

export function createAppQueryClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        // Catalogue data (underlyings, expiries, contracts) changes only when a job finishes,
        // and a finishing job invalidates it explicitly, so a short stale window is free.
        staleTime: 30_000,
        refetchOnWindowFocus: false,
      },
    },
  })

  // Bar arrays are the one payload here big enough to matter: a minute series for a single
  // expiry is tens of thousands of objects, and a user flicking through contracts would
  // otherwise hold every one of them for the default five minutes.
  client.setQueryDefaults(['bars'], { gcTime: 60_000, staleTime: 60_000 })

  // Job state is the opposite: always refetch, never serve a stale run status.
  client.setQueryDefaults(['jobs'], { staleTime: 0 })

  return client
}

const root = document.getElementById('root')
if (!root) {
  throw new Error('index.html is missing the #root element')
}

createRoot(root).render(
  <StrictMode>
    {/*
      attribute="class" matches the '@custom-variant dark (&:is(.dark *))' line shadcn wrote
      into index.css, and the storage key matches public/theme-init.js, which sets the class
      before first paint so there is no light flash.

      This provider is also the chart theme bridge. Chart components call useTheme() from
      next-themes and push resolvedTheme into widget.setTheme(), so the canvas and the app
      chrome always move together off one source of truth.
    */}
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      storageKey="expirymanager-theme"
      disableTransitionOnChange
    >
      <QueryClientProvider client={createAppQueryClient()}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
