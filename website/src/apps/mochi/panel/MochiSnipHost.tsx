/**
 * Screen-capture host for the Mochi panel.
 *
 * Sits beside the vendored ChatApp and performs the capture the seam only
 * *raises*. Two triggers reach it:
 *
 *   1. the global accelerator (shell -> `mochi.onStartSnip`), and
 *   2. an in-panel `api.startCapture()` call from vendored code.
 *
 * The capture itself is KiroCrew's own: `captureScreen()` grabs one frame via
 * getDisplayMedia (the shell registers `setDisplayMediaRequestHandler` and gates
 * the macOS Screen Recording permission — see website/electron/display-media.js),
 * and `SnipOverlay` is the shared drag-to-select surface used by the dashboard.
 * Reusing both means Mochi adds no second capture mechanism and no second
 * permission path, and it works on Windows and Linux — unlike the original,
 * which shelled out to macOS's `screencapture -i`.
 *
 * The pet and panel windows are always-on-top and WILL appear in the frame. That
 * is accepted deliberately: hiding them first would race the frame grab, and the
 * user selects a region from the still afterwards anyway.
 */
import { useCallback, useEffect, useState } from 'react'

import SnipOverlay from '../../../components/SnipOverlay'
import { captureScreen, isScreenSnipSupported } from '../../../hooks/useScreenSnip'
import { deliverCapture, onCaptureRequested } from '../src/mochiApi'
import { reportStat } from './panelBridge'

/** The vendored ChatPanel expects bare base64 (it builds its own data: URL). */
function fileToBase64(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const comma = result.indexOf(',')
      resolve(comma === -1 ? null : result.slice(comma + 1))
    }
    reader.onerror = () => resolve(null)
    reader.readAsDataURL(file)
  })
}

interface ShellApi {
  onStartSnip?: (cb: () => void) => () => void
}

export function MochiSnipHost() {
  const [frame, setFrame] = useState<HTMLCanvasElement | null>(null)
  const [busy, setBusy] = useState(false)

  const begin = useCallback(async () => {
    // Re-entrancy guard: the accelerator can fire again while the picker or the
    // overlay is already up, and a second frame would stack overlays.
    if (busy || frame !== null) return
    if (!isScreenSnipSupported()) {
      // A SILENT return here reads as "the shortcut is broken". Say why.
      console.warn('[mochi] screen capture unavailable: getDisplayMedia is not exposed in this window')
      return
    }
    setBusy(true)
    try {
      const canvas = await captureScreen()
      // null means the user cancelled or the OS refused; display-media.js
      // surfaces the permission dialog in the refused case, but a cancel and a
      // silent rejection look identical from here, so log it either way.
      if (canvas !== null) setFrame(canvas)
      else console.warn('[mochi] screen capture produced no frame (cancelled or refused)')
    } catch (err) {
      console.warn('[mochi] screen capture failed', err)
    } finally {
      setBusy(false)
    }
  }, [busy, frame])

  useEffect(() => {
    const offShell = (window as unknown as { mochi?: ShellApi }).mochi?.onStartSnip?.(() => {
      void begin()
    })
    const offPanel = onCaptureRequested(() => {
      void begin()
    })
    return () => {
      offShell?.()
      offPanel()
    }
  }, [begin])

  if (frame === null) return null

  return (
    <SnipOverlay
      frame={frame}
      onComplete={(file) => {
        setFrame(null)
        void fileToBase64(file).then((base64) => {
          if (base64 !== null) {
            deliverCapture(base64)
            reportStat('screenshot')
          }
        })
      }}
      onCancel={() => setFrame(null)}
    />
  )
}
