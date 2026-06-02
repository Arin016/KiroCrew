import { AnimatePresence, motion } from 'framer-motion'

interface Props {
  open: boolean
  width: number
  dragging?: boolean
  className?: string
  children: React.ReactNode
}

const EASE = [0.32, 0.72, 0, 1] as const
const DUR = 0.25

export default function OverlayDrawer({ open, width, dragging, className, children }: Props) {
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="drawer"
          initial={{ width: 0 }}
          animate={{ width }}
          exit={{ width: 0 }}
          transition={dragging ? { duration: 0 } : { duration: DUR, ease: EASE }}
          className={`shrink-0 py-2 overflow-hidden ${className || ''}`}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
