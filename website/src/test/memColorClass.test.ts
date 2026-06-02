import { describe, it, expect } from 'vitest'
import { memColorClass } from '../App'

describe('memColorClass', () => {
  it('returns text-accent (green) for usage below 70%', () => {
    expect(memColorClass(0)).toBe('text-accent')
    expect(memColorClass(0.5)).toBe('text-accent')
    expect(memColorClass(0.69)).toBe('text-accent')
    expect(memColorClass(0.7)).toBe('text-accent')
  })

  it('returns text-warn (yellow) for usage between 70-90%', () => {
    expect(memColorClass(0.71)).toBe('text-warn')
    expect(memColorClass(0.8)).toBe('text-warn')
    expect(memColorClass(0.9)).toBe('text-warn')
  })

  it('returns text-danger (red) for usage above 90%', () => {
    expect(memColorClass(0.91)).toBe('text-danger')
    expect(memColorClass(0.95)).toBe('text-danger')
    expect(memColorClass(1.0)).toBe('text-danger')
  })
})
