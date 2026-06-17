import { describe, it, expect } from 'vitest'
import { humanizeMicError } from '../hooks/mic'

describe('humanizeMicError', () => {
  it('maps permission denial', () => {
    expect(humanizeMicError({ name: 'NotAllowedError' })).toMatch(/permission denied/i)
  })
  it('maps a missing device', () => {
    expect(humanizeMicError({ name: 'NotFoundError' })).toMatch(/no microphone/i)
  })
  it('maps a device already in use', () => {
    expect(humanizeMicError({ name: 'NotReadableError' })).toMatch(/another app/i)
  })
  it('falls back for unknown errors', () => {
    expect(humanizeMicError(new Error('weird'))).toMatch(/could not start/i)
  })
})
