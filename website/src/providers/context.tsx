import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { getAdapter } from './registry'
import type { ProviderAdapter, ProviderId } from './types'

const fallback = getAdapter('acp')

const ProviderContext = createContext<ProviderAdapter>(fallback)

export function ProviderProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery<any>({
    queryKey: ['kiroclawConfig'],
    queryFn: () => api.kiroclawConfig(),
    staleTime: 30_000,
  })
  const providerId = (data?.agent?.provider || 'acp') as ProviderId
  const adapter = useMemo(() => getAdapter(providerId), [providerId])
  return <ProviderContext.Provider value={adapter}>{children}</ProviderContext.Provider>
}

export function useProvider(): ProviderAdapter {
  return useContext(ProviderContext)
}
