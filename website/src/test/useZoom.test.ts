import { renderHook, act } from '@testing-library/react'
import { useZoom } from '../hooks/useZoom'

beforeEach(() => localStorage.clear())

test('defaults to 100%', () => {
  const { result } = renderHook(() => useZoom())
  expect(result.current.zoom).toBe(100)
})

test('zoomIn/zoomOut changes by 10%', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.zoomIn())
  expect(result.current.zoom).toBe(110)
  act(() => result.current.zoomOut())
  expect(result.current.zoom).toBe(100)
})

test('clamps to 80–150 range', () => {
  const { result } = renderHook(() => useZoom())
  for (let i = 0; i < 10; i++) act(() => result.current.zoomOut())
  expect(result.current.zoom).toBe(80)
  for (let i = 0; i < 20; i++) act(() => result.current.zoomIn())
  expect(result.current.zoom).toBe(150)
})

test('reset returns to 100%', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.zoomIn())
  act(() => result.current.reset())
  expect(result.current.zoom).toBe(100)
})

test('persists to localStorage', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.zoomIn())
  expect(localStorage.getItem('mc-zoom')).toBe('110')
})

test('reads persisted zoom from localStorage', () => {
  localStorage.setItem('mc-zoom', '120')
  const { result } = renderHook(() => useZoom())
  expect(result.current.zoom).toBe(120)
})

test('defaults font family to sans', () => {
  const { result } = renderHook(() => useZoom())
  expect(result.current.family).toBe('sans')
})

test('setFontFamily updates state and persists', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.setFontFamily('mono'))
  expect(result.current.family).toBe('mono')
  expect(localStorage.getItem('mc-font-family')).toBe('mono')
})

test('cycleFamily rotates sans → mono → system → sans', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('mono')
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('system')
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('sans')
})
