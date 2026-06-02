import { useEffect, useRef, useCallback, useState } from 'react'

export type WatchStatus = 'idle' | 'connecting' | 'open' | 'error'

/** Subscribe to SSE file-change events at GET /api/file-watch?path=... */
export function useFileWatch(
  filePath: string | null,
  onContent: (content: string) => void,
) {
  const cbRef = useRef(onContent)
  cbRef.current = onContent
  const esRef = useRef<EventSource | null>(null)
  const [status, setStatus] = useState<WatchStatus>('idle')

  const stop = useCallback(() => {
    esRef.current?.close()
    esRef.current = null
    setStatus('idle')
  }, [])

  useEffect(() => {
    if (!filePath) { setStatus('idle'); return }

    setStatus('connecting')
    const es = new EventSource('/api/file-watch?path=' + encodeURIComponent(filePath))
    esRef.current = es

    es.onopen = () => setStatus('open')
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data)
        if (data.content != null) cbRef.current(data.content)
      } catch { /* ignore parse errors */ }
    }
    es.onerror = () => setStatus('error')

    return () => {
      es.close()
      esRef.current = null
      setStatus('idle')
    }
  }, [filePath])

  return { stop, status }
}
