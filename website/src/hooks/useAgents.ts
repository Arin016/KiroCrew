import { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { KiroClawAgent } from '../components/AgentSelector'

export function useAgents(refreshTrigger: number) {
  const [agents, setAgents] = useState<KiroClawAgent[]>([])
  const [defaultAgent, setDefaultAgent] = useState('')
  const hasSynced = useRef(false)

  useEffect(() => {
    let cancelled = false
    const fetchAgents = () =>
      api.kiroclawAgents().then(d => {
        if (cancelled) return
        setAgents(d.agents || [])
        setDefaultAgent(d.default_agent || '')
      }).catch(() => {})

    if (!hasSynced.current) {
      hasSynced.current = true
      api.syncKiroclawAgents().then(fetchAgents).catch(fetchAgents)
    } else {
      fetchAgents()
    }

    return () => { cancelled = true }
  }, [refreshTrigger])

  return { agents, defaultAgent }
}
