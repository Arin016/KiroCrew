import React, { useState, useCallback, type RefObject } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDispatch } from 'react-redux'
import { switchSlot } from '../store/chatSlice'

/** Minimal agent shape for hit-testing — all scene agent types satisfy this */
export interface SceneAgent {
  id: string; name: string; x: number; y: number; running: boolean; detail: string
  kind: 'slot' | 'cron' | 'spawn'
}

export interface SceneTooltipTheme {
  active: string   // e.g. "Grinding PRs"
  idle: string     // e.g. "Waiting for CR approval"
}

interface TooltipState {
  x: number; y: number; agent: SceneAgent
}

/**
 * Shared click-to-chat + tooltip for all Worlds scenes.
 * @param canvasRef - ref to the pixel canvas element
 * @param agentsRef - ref to the scene's agent array
 * @param W - scene width in logical pixels
 * @param H - scene height in logical pixels
 * @param theme - scene-specific tooltip messages
 * @param hitRadius - hit-test radius in logical pixels (default 10)
 */
export function useSceneInteraction(
  canvasRef: RefObject<HTMLCanvasElement | null>,
  agentsRef: RefObject<SceneAgent[]>,
  W: number, H: number,
  theme: SceneTooltipTheme,
  hitRadius = 10,
  extraLine?: (agent: SceneAgent) => React.ReactNode,
) {
  const navigate = useNavigate()
  const dispatch = useDispatch()
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)

  const getAgentAt = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cv = canvasRef.current
    if (!cv) return undefined
    const rect = cv.getBoundingClientRect()
    const mx = (e.clientX - rect.left) / rect.width * W
    const my = (e.clientY - rect.top) / rect.height * H
    return agentsRef.current?.find(a => Math.abs(a.x - mx) < hitRadius && Math.abs(a.y - my) < hitRadius)
  }, [canvasRef, agentsRef, W, H, hitRadius])

  const onMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const a = getAgentAt(e)
    if (a) {
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return
      setTooltip({ x: e.clientX - rect.left + 12, y: e.clientY - rect.top - 50, agent: a })
    } else {
      setTooltip(null)
    }
  }, [getAgentAt, canvasRef])

  const onMouseLeave = useCallback(() => setTooltip(null), [])

  const onClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const a = getAgentAt(e)
    if (a && a.kind === 'slot') {
      const slotKey = a.id.replace(/^slot-/, '')
      dispatch(switchSlot(slotKey) as any)
      navigate('/chat')
    }
  }, [getAgentAt, dispatch, navigate])

  const tooltipEl = tooltip ? (
    <div style={{ position: 'absolute', left: tooltip.x, top: tooltip.y, background: '#111', border: '1px solid #555', borderRadius: 4, padding: '4px 8px', fontSize: 11, color: '#ccc', pointerEvents: 'none', whiteSpace: 'nowrap', zIndex: 10 }}>
      <div style={{ color: '#f90', fontWeight: 'bold' }}>{tooltip.agent.name}</div>
      <div>{tooltip.agent.running ? `🟢 ${theme.active}` : `🟡 ${theme.idle}`}</div>
      {extraLine && extraLine(tooltip.agent)}
    </div>
  ) : null

  return {
    canvasProps: { onMouseMove, onMouseLeave, onClick, style: { cursor: tooltip?.agent.kind === 'slot' ? 'pointer' : 'default' } },
    tooltipEl,
  }
}
