/**
 * Test mock for @radix-ui/react-context-menu.
 *
 * Stateful: Content shows after onContextMenu fires on the Trigger child.
 * Items respond to fireEvent.click directly.
 */
import React, { useState, useContext, createContext } from 'react'

const Ctx = createContext<{ open: boolean; setOpen: (v: boolean) => void }>({ open: false, setOpen: () => {} })

export const Root: React.FC<any> = ({ children }) => {
  const [open, setOpen] = useState(false)
  return <Ctx.Provider value={{ open, setOpen }}>{children}</Ctx.Provider>
}

export const Trigger = React.forwardRef<HTMLElement, any>(({ children, asChild, ...props }, ref) => {
  const { open, setOpen } = useContext(Ctx)
  const handleContextMenu = (e: any) => { e.preventDefault?.(); setOpen(true); props.onContextMenu?.(e) }
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as React.ReactElement<any>, { ...props, ref, onContextMenu: handleContextMenu, 'data-state': open ? 'open' : 'closed' })
  }
  return <span ref={ref} {...props} onContextMenu={handleContextMenu} data-state={open ? 'open' : 'closed'}>{children}</span>
})

export const Portal: React.FC<any> = ({ children }) => <>{children}</>
export const Content = React.forwardRef<HTMLDivElement, any>(({ children, className, ...props }, ref) => {
  const { open } = useContext(Ctx)
  if (!open) return null
  return <div ref={ref} role="menu" className={className} {...props}>{children}</div>
})
export const Item = React.forwardRef<HTMLDivElement, any>(({ children, className, onSelect, ...props }, ref) => {
  const { setOpen } = useContext(Ctx)
  return (
    <div ref={ref} role="menuitem" className={className} {...props}
      onClick={e => { props.onClick?.(e); onSelect?.(e); setOpen(false) }}>
      {children}
    </div>
  )
})
export const Separator = React.forwardRef<HTMLDivElement, any>((props, ref) => <div ref={ref} role="separator" {...props} />)
export const Group: React.FC<any> = ({ children }) => <>{children}</>
export const Sub: React.FC<any> = ({ children }) => <>{children}</>
export const RadioGroup: React.FC<any> = ({ children }) => <>{children}</>
