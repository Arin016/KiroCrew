import { useState, useRef, useCallback, useEffect } from 'react'
import { api } from '../api/client'
import { streamingSupported, useStreamingStt } from './useStreamingStt'

function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  for (const mt of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg']) {
    if (MediaRecorder.isTypeSupported(mt)) return mt
  }
  return ''
}

/** True if browser supports mic recording. */
export const voiceInputSupported =
  typeof navigator !== 'undefined' &&
  typeof navigator.mediaDevices !== 'undefined' &&
  typeof navigator.mediaDevices.getUserMedia === 'function' &&
  pickMimeType() !== ''

interface Opts {
  streaming?: boolean
  onPartial?: (text: string) => void
}

export function useVoiceInput(onText: (text: string) => void, opts: Opts = {}) {
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const mediaRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const startingRef = useRef(false)

  const streamEnabled = !!opts.streaming && streamingSupported
  const optsPartial = opts.onPartial
  const streamOnPartial = useCallback(
    (text: string) => { optsPartial?.(text) },
    [optsPartial],
  )
  const streamOnFinal = useCallback(
    (text: string) => { if (text) onText(text) },
    [onText],
  )
  // Destructure individual members so downstream useCallback deps track
  // stable references (start/stop/recording) instead of the hook's
  // always-new return object literal, preventing memoization churn.
  const { recording: streamRecording, start: streamStart, stop: streamStop } = useStreamingStt({
    onPartial: streamOnPartial,
    onFinal: streamOnFinal,
  })

  // Stop any in-flight streaming session when the user toggles streaming off
  // mid-session. Without this, the WebSocket + Transcribe session leak until
  // unmount — Transcribe bills for the whole idle window. Must live here
  // (not ChatPage) to read `streamRecording` directly: routing through the
  // returned `recording` property is racy because `useVoiceInput` flips it
  // to the batch `recording` (false) on the same render where `streamEnabled`
  // goes false, so the caller's `voice.recording` is already false.
  useEffect(() => {
    if (!streamEnabled && streamRecording) streamStop()
  }, [streamEnabled, streamRecording, streamStop])

  const stopStream = useCallback(() => {
    if (mediaRef.current) {
      mediaRef.current.stream.getTracks().forEach(t => t.stop())
      if (mediaRef.current.state === 'recording') mediaRef.current.stop()
      mediaRef.current = null
    }
  }, [])

  // Cleanup on unmount — stop mic stream
  useEffect(() => () => { stopStream() }, [stopStream])

  const start = useCallback(async () => {
    if (streamEnabled) { await streamStart(); return }
    if (!voiceInputSupported || startingRef.current) return
    startingRef.current = true
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mimeType = pickMimeType()
      const mr = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      chunksRef.current = []
      mr.ondataavailable = e => { if (e.data.size) chunksRef.current.push(e.data) }
      mr.onstop = async () => {
        stream.getTracks().forEach(t => t.stop())
        const ext = mimeType.includes('mp4') ? 'mp4' : mimeType.includes('ogg') ? 'ogg' : 'webm'
        const blob = new Blob(chunksRef.current, { type: mimeType || 'audio/webm' })
        if (blob.size < 100) return
        setTranscribing(true)
        try {
          const res = await api.sttTranscribe(blob, ext)
          if (res.error) console.error('[voice] STT error:', res.error)
          else if (res.text) onText(res.text)
        } catch (err) { console.error('[voice] transcription failed:', err) }
        setTranscribing(false)
      }
      mr.start()
      mediaRef.current = mr
      setRecording(true)
    } catch { /* mic denied or unavailable */ }
    startingRef.current = false
  }, [onText, streamEnabled, streamStart])

  const stop = useCallback(() => {
    if (streamEnabled) { streamStop(); return }
    if (mediaRef.current?.state === 'recording') {
      mediaRef.current.stop()
      mediaRef.current = null
    }
    setRecording(false)
  }, [streamEnabled, streamStop])

  const isRecording = streamEnabled ? streamRecording : recording
  const toggle = useCallback(() => { isRecording ? stop() : start() }, [isRecording, start, stop])

  return { recording: isRecording, transcribing, toggle }
}
