import { useCallback, useEffect } from 'react'
import { Star, X } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import MarkdownRenderer from './MarkdownRenderer'

interface RegistryServer {
  id: string; installed: string; title: string; tier: string; description: string
}

interface McpDetailModalProps {
  server: RegistryServer | null
  onDismiss: () => void
  actions: React.ReactNode
}

const SPRING = { type: 'spring' as const, stiffness: 500, damping: 35 }

export default function McpDetailModal({ server, onDismiss, actions }: McpDetailModalProps) {
  const dismiss = useCallback(() => onDismiss(), [onDismiss])

  useEffect(() => {
    if (!server) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [server, dismiss])

  return (
    <AnimatePresence>
      {server && (
        <>
          <motion.div
            className="fixed inset-0 bg-black/40 z-[100]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={dismiss}
          />
          <div className="fixed inset-0 z-[101] flex items-center justify-center p-8 pointer-events-none">
            <motion.div
              role="dialog"
              aria-modal="true"
              aria-labelledby={`mcp-modal-title-${server.id}`}
              layoutId={`mcp-card-${server.id}`}
              transition={SPRING}
              className="bg-card border border-border rounded-xl shadow-2xl w-full max-w-[640px] h-[70vh] max-h-[90vh] flex flex-col pointer-events-auto overflow-hidden"
            >
              {/* Header */}
              <div className="flex items-start justify-between gap-3 p-5 pb-3 shrink-0">
                <div className="min-w-0">
                  <div id={`mcp-modal-title-${server.id}`} className="text-lg font-bold text-text-strong leading-tight">{server.title || server.id}</div>
                  <div className="text-[12px] text-muted font-mono mt-1">{server.id}</div>
                  <div className="flex gap-1.5 mt-2">
                    {server.tier === 'Recommended' && <span className="px-1.5 py-[1px] rounded-full text-[11px] font-bold bg-accent/15 text-accent border border-accent/30"><Star className="lucide-inline" /> recommended</span>}
                    {server.tier === 'Supported' && <span className="px-1.5 py-[1px] rounded-full text-[11px] font-bold bg-muted/15 text-muted border border-muted/30">supported</span>}
                  </div>
                </div>
                <button aria-label="Close" className="shrink-0 p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer" onClick={dismiss}><X size={18} /></button>
              </div>
              {/* Description */}
              <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden px-5 border-t border-border">
                <div className="pt-4 pb-4 text-sm leading-relaxed">
                  {/* MarkdownRenderer sanitizes HTML via rehypeSanitize plugin */}
                  {server.description ? <MarkdownRenderer content={server.description} /> : <p className="text-muted">No description available.</p>}
                </div>
              </div>
              {/* Actions — pinned at bottom */}
              <div className="px-5 py-3 border-t border-border/50 shrink-0">
                {actions}
              </div>
            </motion.div>
          </div>
        </>
      )}
    </AnimatePresence>
  )
}
