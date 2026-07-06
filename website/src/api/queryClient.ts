import { QueryClient } from '@tanstack/react-query'

/**
 * Single shared QueryClient instance. Exported so non-React modules (notably
 * api/client.ts's warm-path refresh recovery) can invalidate cached queries
 * such as ['auth-me'] without holding a React context handle. main.tsx passes
 * this same instance to QueryClientProvider, so useQueryClient() hits it too.
 */
export const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
})
