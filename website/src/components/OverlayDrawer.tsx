import { AnimatePresence, motion } from 'framer-motion'

interface Props {
  open: boolean
  width: number
  dragging?: boolean
  /** When true, the panel fades as it collapses (content fade) — the visible
   *  border-box morph into the expand button is handled by a dedicated element
   *  in ChatPage; this just collapses the panel cleanly for layout reflow. */
  morph?: boolean
  className?: string
  children: React.ReactNode
}

const EASE = [0.32, 0.72, 0, 1] as const
const DUR = 0.25

export default function OverlayDrawer({ open, width, dragging, morph, className, children }: Props) {
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="drawer"
          initial={morph ? { width: 0, opacity: 0 } : { width: 0 }}
          animate={morph ? { width, opacity: 1 } : { width }}
          exit={morph ? { width: 0, opacity: 0 } : { width: 0 }}
          transition={
            dragging
              ? { duration: 0 }
              : morph
                ? { width: { duration: 0.32, ease: EASE }, opacity: { duration: 0.12, ease: EASE } }
                : { duration: DUR, ease: EASE }
          }
          className={`shrink-0 py-2 overflow-hidden ${className || ''}`}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
