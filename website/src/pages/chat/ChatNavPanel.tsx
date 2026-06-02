import { motion } from 'framer-motion'
import { X, Link2, List } from 'lucide-react'
import type { ExtractedLink } from '../../utils/extractChatLinks'
import type { ChatSection } from '../../hooks/useChatNavigation'
import Clickable from '../../components/Clickable'

interface ChatNavPanelProps {
  links: ExtractedLink[]
  sections: ChatSection[]
  onScrollToSection: (displayIdx: number) => void
  onClose: () => void
  searchOpen?: boolean
  resolving?: boolean
}

const TYPE_COLORS: Record<string, string> = {
  cr: 'bg-cyan-500/15 text-cyan-400',
  other: 'bg-muted/15 text-muted',
}

const TYPE_LABELS: Record<string, string> = {
  cr: 'CR',
  other: 'Link',
}

export default function ChatNavPanel({ links, sections, onScrollToSection, onClose, searchOpen, resolving }: ChatNavPanelProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0, top: searchOpen ? '6rem' : '3.5rem' }}
      exit={{ opacity: 0, y: -8 }}
      transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
      className="absolute right-4 z-30 w-[280px] max-h-[60vh] rounded-lg bg-bg-elevated border border-border shadow-lg flex flex-col overflow-hidden"
    >
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <span className="text-[13px] font-semibold text-text-strong">Navigation</span>
        <Clickable className="p-1 rounded text-muted hover:text-text transition-colors" onClick={onClose} aria-label="Close navigation panel">
          <X size={14} />
        </Clickable>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 flex flex-col gap-2">
        {/* Key Resources */}
        <div>
          <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-semibold mb-1.5">
            <Link2 size={11} /> Resources
            {resolving && <span className="ml-auto text-[10px] text-accent animate-pulse">resolving…</span>}
          </div>
          {links.length > 0 ? (
            <div className="flex flex-col gap-0.5">
              {links.map((link, i) => (
                <a
                  key={i}
                  href={link.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-2 px-2 py-1 rounded hover:bg-bg-hover transition-colors no-underline group"
                >
                  <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${TYPE_COLORS[link.type] || TYPE_COLORS.other}`}>
                    {TYPE_LABELS[link.type] || 'Link'}
                  </span>
                  <span className="text-[12px] text-text truncate group-hover:text-accent transition-colors">
                    {link.label}
                  </span>
                </a>
              ))}
            </div>
          ) : (
            <span className="text-muted text-[12px] px-2">No links found</span>
          )}
        </div>

        {/* Divider */}
        <div className="border-b border-border" />

        {/* Conversation Outline */}
        <div>
          <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-semibold mb-1.5">
            <List size={11} /> Outline
          </div>
          {sections.length > 0 ? (
            <div className="flex flex-col gap-0.5">
              {sections.map((section, i) => (
                <Clickable
                  key={i}
                  className="text-left text-[12px] leading-tight px-2 py-1.5 rounded text-text hover:bg-bg-hover hover:text-accent transition-colors cursor-pointer truncate"
                  onClick={() => onScrollToSection(section.displayIdx)}
                  title={section.label}
                >
                  <span className="text-muted mr-1.5">{i + 1}.</span>
                  {section.label}
                </Clickable>
              ))}
            </div>
          ) : (
            <span className="text-muted text-[12px] px-2">Start chatting to see sections</span>
          )}
        </div>
      </div>
    </motion.div>
  )
}
