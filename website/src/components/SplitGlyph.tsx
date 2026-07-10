import { PanelRight, PanelBottom } from 'lucide-react'

/** Split-direction icon: a right panel = split right, a bottom panel = split down. */
export function SplitGlyph({ down }: { down?: boolean }) {
  return down ? <PanelBottom size={12} /> : <PanelRight size={12} />
}
