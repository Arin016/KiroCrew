/**
 * AppPage — loads an installed app via AppHost (dynamic ESM import).
 */
import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { api } from '../api/client'
import AppHost from '../components/AppHost'

export default function AppPage() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const [app, setApp] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!name) return
    let redirecting = false
    api.getApp(name)
      .then((data: any) => {
        // Builtin apps have native routes — redirect there instead of AppHost
        if (data?.origin === 'builtin' && data?.manifest?.ui?.pages?.[0]?.route) {
          redirecting = true
          navigate(data.manifest.ui.pages[0].route, { replace: true })
          return
        }
        setApp(data)
      })
      .catch(() => setApp(null))
      .finally(() => {
        if (!redirecting) setLoading(false)
      })
  }, [name, navigate])

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted text-sm">
        <Loader2 size={16} className="animate-spin mr-2" /> Loading app…
      </div>
    )
  }

  return <AppHost app={app} />
}
