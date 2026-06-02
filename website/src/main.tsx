import React, { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store } from './store'
import { BrandingProvider } from './hooks/useBranding'
import { ProviderProvider } from './providers'
import { ThemeProvider } from './hooks/useTheme'
import { initRum } from './rum'
import App from './App'
import 'katex/dist/katex.min.css'
import './index.css'
// Register shared modules for federated app bundles (must be before any app loads)
import './app-sdk/shared-modules'

// Initialize RUM as early as possible
initRum(__APP_VERSION__)

// Accessibility: runtime DOM scanning in dev mode (logs violations to console)
if (import.meta.env.DEV) {
  import('react-dom').then(ReactDOM => import('@axe-core/react').then(axe => axe.default(React, ReactDOM, 1000)))
}

const WorldsPopout = lazy(() => import('./pages/WorldsPopout'))

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/worlds-popout" element={<BrandingProvider><ProviderProvider><Suspense fallback={null}><WorldsPopout /></Suspense></ProviderProvider></BrandingProvider>} />
              <Route path="*" element={<BrandingProvider><ProviderProvider><App /></ProviderProvider></BrandingProvider>} />
            </Routes>
          </BrowserRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  </StrictMode>,
)
