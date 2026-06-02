/**
 * Shared text rendering for Worlds scenes.
 * Provides a high-DPI text overlay canvas and auto-sized text labels.
 */

import type { CSSProperties } from 'react'
import { SCENE_SCALE, SCENE_LAYOUT_SCALE } from '../pages/scenes/config'

const FONT_FAMILY = '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif'
const TEXT_SCALE = SCENE_SCALE / SCENE_LAYOUT_SCALE
const BASE_SIZE = 14 * TEXT_SCALE

type TextRole = 'title' | 'name' | 'status' | 'detail' | 'label' | 'spell'
const ROLE_SCALE: Record<TextRole, number> = {
  title: 1.1,
  spell: 0.85,
  name: 0.65,
  status: 0.55,
  detail: 0.48,
  label: 0.43,
}

/** Get a font string for a given role and optional weight */
export function sceneFont(role: TextRole, weight: '' | 'bold' = ''): string {
  const size = BASE_SIZE * ROLE_SCALE[role]
  return weight ? `${weight} ${size}px ${FONT_FAMILY}` : `${size}px ${FONT_FAMILY}`
}

/** Get the computed font size for a role */
export function sceneFontSize(role: TextRole): number {
  return BASE_SIZE * ROLE_SCALE[role]
}

/**
 * Initialize a text overlay canvas for high-DPI rendering.
 * Returns the 2D context. Call once during effect setup.
 */
export function initTextCanvas(canvas: HTMLCanvasElement, w: number, h: number, scale: number): CanvasRenderingContext2D {
  const dpr = window.devicePixelRatio || 1
  const cssW = w * scale
  const cssH = h * scale
  canvas.width = cssW * dpr
  canvas.height = cssH * dpr
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Failed to get 2d context for text canvas')
  ctx.imageSmoothingEnabled = true
  ctx.scale(dpr, dpr)
  return ctx
}

/**
 * Draw text with an optional auto-sized background box.
 * Both background and text are drawn on the text canvas (T) for correct DPI alignment.
 */
export function drawLabel(
  T: CanvasRenderingContext2D,
  text: string,
  x: number, y: number,
  opts: {
    role: TextRole
    weight?: '' | 'bold'
    color: string
    bgColor?: string
    align?: CanvasTextAlign
    padX?: number
    padY?: number
    scale: number
  },
) {
  const { role, weight = '', color, bgColor, align = 'start', padX = 3, padY = 2, scale: S } = opts
  T.font = sceneFont(role, weight)
  T.fillStyle = color
  T.textAlign = align
  T.textBaseline = 'middle'

  if (bgColor) {
    const metrics = T.measureText(text)
    const tw = metrics.width
    const fontSize = sceneFontSize(role)
    const bh = fontSize + padY * 2 * S
    let bx = x
    if (align === 'center') bx = x - tw / 2
    else if (align === 'end') bx = x - tw
    T.fillStyle = bgColor
    T.fillRect(bx - padX * S, y - bh / 2, tw + padX * 2 * S, bh)
    T.fillStyle = color
  }

  T.fillText(text, x, y)
  T.textAlign = 'start'
  T.textBaseline = 'alphabetic'
}

/** CSS props for the text overlay canvas element */
export const TEXT_CANVAS_STYLE: CSSProperties = {
  position: 'absolute',
  inset: 0,
  width: '100%',
  height: '100%',
  pointerEvents: 'none',
  borderRadius: 8,
}

/** CSS props for the scene container div */
export const SCENE_CONTAINER_STYLE = (w: number, h: number): CSSProperties => ({
  position: 'relative',
  maxWidth: '100%',
  aspectRatio: `${w}/${h}`,
})

/** CSS props for the pixel art canvas */
export const PIXEL_CANVAS_STYLE: CSSProperties = {
  imageRendering: 'pixelated',
  border: '2px solid var(--accent, #f90)',
  cursor: 'pointer',
  width: '100%',
  maxHeight: '100%',
  objectFit: 'contain',
  borderRadius: 8,
}
