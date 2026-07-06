/**
 * Test mock for @radix-ui/react-dropdown-menu.
 *
 * Stateful: Content is hidden until Trigger is clicked, then items
 * respond to fireEvent.click directly (no pointer-event gating).
 */
import React, { useState, useContext, createContext } from 'react'

const Ctx = createContext<{ open: boolean; setOpen: (v: boolean) => void }>({ open: false, setOpen: () => {} })

export const Root: React.FC<any> = ({ children, open: controlledOpen, onOpenChange }) => {
  const [internal, setInternal] = useState(false)
  const open = controlledOpen ?? internal
  const setOpen = (v: boolean) => { setInternal(v); onOpenChange?.(v) }
  return <Ctx.Provider value={{ open, setOpen }}>{children}</Ctx.Provider>
}

export const Trigger = React.forwardRef<HTMLButtonElement, any>(({ children, asChild, ...props }, ref) => {
  const { open, setOpen } = useContext(Ctx)
  const handleClick = (e: any) => { setOpen(!open); props.onClick?.(e) }
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as React.ReactElement<any>, { ...props, ref, onClick: handleClick, 'data-state': open ? 'open' : 'closed' })
  }
  return <button ref={ref} {...props} onClick={handleClick} data-state={open ? 'open' : 'closed'}>{children}</button>
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
export const Label = React.forwardRef<HTMLDivElement, any>(({ children, ...props }, ref) => <div ref={ref} {...props}>{children}</div>)
export const Group: React.FC<any> = ({ children }) => <>{children}</>
export const Sub: React.FC<any> = ({ children }) => {
  const [open, setOpen] = useState(false)
  return <Ctx.Provider value={{ open, setOpen }}>{children}</Ctx.Provider>
}
export const SubTrigger = React.forwardRef<HTMLDivElement, any>(({ children, className, ...props }, ref) => {
  const { setOpen } = useContext(Ctx)
  return <div ref={ref} role="menuitem" className={className} {...props} onMouseEnter={() => setOpen(true)}>{children}</div>
})
export const SubContent = React.forwardRef<HTMLDivElement, any>(({ children, className, ...props }, ref) => {
  const { open } = useContext(Ctx)
  if (!open) return null
  return <div ref={ref} role="menu" className={className} {...props}>{children}</div>
})
export const RadioGroup: React.FC<any> = ({ children }) => <>{children}</>
