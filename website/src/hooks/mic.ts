// Shared microphone helpers for voice input.
//
// Centralizes the bits both capture paths (batch `useVoiceInput` and streaming
// `useStreamingStt`) need: a persisted preferred input device, getUserMedia
// constraints honoring it, human-readable error mapping, a live input-level
// meter, and device enumeration for the settings picker.

const MIC_DEVICE_KEY = 'mc-mic-device-id'

/** Preferred input deviceId. Empty string means "system default". */
export function getPreferredMicId(): string {
  try {
    return localStorage.getItem(MIC_DEVICE_KEY) || ''
  } catch {
    return ''
  }
}

export function setPreferredMicId(id: string): void {
  try {
    if (id) localStorage.setItem(MIC_DEVICE_KEY, id)
    else localStorage.removeItem(MIC_DEVICE_KEY)
  } catch {
    /* localStorage unavailable — fall back to default device */
  }
}

/**
 * getUserMedia audio constraints honoring the saved device. Uses `ideal`
 * (not `exact`) so an unplugged/removed device falls back to the default
 * instead of throwing OverconstrainedError.
 */
export function micAudioConstraints(): MediaStreamConstraints {
  const id = getPreferredMicId()
  return { audio: id ? { deviceId: { ideal: id } } : true }
}

/** Map a getUserMedia rejection to a short, human-readable message. */
export function humanizeMicError(e: unknown): string {
  const name = (e as { name?: string } | null)?.name || ''
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone permission denied. Allow mic access in your OS and browser settings.'
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone found. Connect one or pick a different device in Settings.'
    case 'NotReadableError':
    case 'AbortError':
      return 'Microphone is unavailable — another app may be using it.'
    default:
      return 'Could not start the microphone.'
  }
}

/**
 * Attach a live RMS level meter to a stream. Calls `onLevel` with a value in
 * [0, 1], throttled to ~15fps and only when the (quantized) level changes, so
 * it bounds re-renders of the consuming component during recording. Returns a
 * stop function that tears down the RAF loop + AudioContext and emits 0.
 */
export function createLevelMeter(stream: MediaStream, onLevel: (v: number) => void): () => void {
  let stopped = false
  let raf = 0
  let lastEmit = 0
  let lastQuantized = -1
  let ctx: AudioContext | null = null
  try {
    ctx = new AudioContext()
    const source = ctx.createMediaStreamSource(stream)
    const analyser = ctx.createAnalyser()
    analyser.fftSize = 512
    source.connect(analyser)
    // Worklet/analyser output is never routed to destination — no echo.
    const buf = new Uint8Array(analyser.frequencyBinCount)
    const tick = (t: number) => {
      if (stopped) return
      analyser.getByteTimeDomainData(buf)
      let sum = 0
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128
        sum += v * v
      }
      // Light gain so ordinary speech visibly moves the meter.
      const rms = Math.min(1, Math.sqrt(sum / buf.length) * 2.2)
      const q = Math.round(rms * 25) / 25
      if (t - lastEmit > 66 && q !== lastQuantized) {
        lastQuantized = q
        lastEmit = t
        onLevel(q)
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
  } catch {
    /* AudioContext unavailable — recording still works, just no meter */
  }
  return () => {
    stopped = true
    if (raf) cancelAnimationFrame(raf)
    try {
      ctx?.close()
    } catch {
      /* already closed */
    }
    onLevel(0)
  }
}

/**
 * List audio input devices. Device labels are only populated after the page
 * has been granted microphone access at least once; before that, labels are
 * empty strings (callers should show a fallback name or a grant affordance).
 */
export async function listMicrophones(): Promise<MediaDeviceInfo[]> {
  try {
    if (!navigator.mediaDevices?.enumerateDevices) return []
    const devices = await navigator.mediaDevices.enumerateDevices()
    return devices.filter(d => d.kind === 'audioinput')
  } catch {
    return []
  }
}
