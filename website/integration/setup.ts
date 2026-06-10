import '@testing-library/jest-dom'
import { server } from './mocks/server'

// Storage polyfill: on Node 22+ the runtime ships a native `localStorage`
// (gated by --localstorage-file) that vitest exposes globally; on Node 25 it
// shadows jsdom's spec-complete Storage with an incomplete one missing
// `.clear()`/`.key()`, breaking every test that does `localStorage.clear()` in
// beforeEach. Install a deterministic in-memory Storage so tests never depend
// on the Node/jsdom version's storage quirks.
// Methods live on a shared prototype so tests can spy on
// `Storage.prototype.setItem` (e.g. to simulate quota errors) and have it
// affect the polyfilled instances, matching real DOM Storage semantics.
const _StoragePrototype = (typeof Storage !== 'undefined' && Storage.prototype) || ({} as any)
_StoragePrototype.clear = function (this: any): void { this._m.clear() }
_StoragePrototype.getItem = function (this: any, k: string): string | null {
  return this._m.has(k) ? this._m.get(k) : null
}
_StoragePrototype.key = function (this: any, i: number): string | null {
  return Array.from(this._m.keys())[i] ?? null
}
_StoragePrototype.removeItem = function (this: any, k: string): void { this._m.delete(k) }
_StoragePrototype.setItem = function (this: any, k: string, v: string): void { this._m.set(k, String(v)) }
function _makeStorage(): Storage {
  const inst: any = Object.create(_StoragePrototype)
  inst._m = new Map<string, string>()
  Object.defineProperty(inst, 'length', { get() { return this._m.size }, configurable: true })
  return inst as Storage
}
for (const prop of ['localStorage', 'sessionStorage'] as const) {
  const store = _makeStorage()
  for (const target of [globalThis, typeof window !== 'undefined' ? window : undefined]) {
    if (target) {
      try {
        Object.defineProperty(target, prop, { value: store, writable: true, configurable: true })
      } catch {
        // ignore — some targets lock the property; the global definition wins
      }
    }
  }
}

// jsdom polyfill: window.matchMedia (used by useTheme → useSessionPalette)
if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

// jsdom polyfill: IntersectionObserver. Used by:
//   - WidgetFrame (lazy-load gate — needs to fire so `visible` flips true
//     and srcdoc/iframe gets built; otherwise theme/srcdoc tests inspect
//     an empty wrapper)
//   - usePaginatedMessages (top-of-list sentinel for load-more —
//     immediate-fire is safe because the hook guards on
//     visibleItems.length >= allItems.length and short-circuits when
//     there's nothing more to load)
// Fires synchronously on `observe()` with isIntersecting: true so both
// behaviours work in the same tests.
if (typeof window !== 'undefined' && !(window as unknown as { IntersectionObserver?: unknown }).IntersectionObserver) {
  class StubIntersectionObserver {
    private readonly cb: IntersectionObserverCallback
    constructor(cb: IntersectionObserverCallback) { this.cb = cb }
    observe(target: Element) {
      // Fire once with isIntersecting=true. WidgetFrame disconnects after
      // the first hit; usePaginatedMessages re-arms the same target which
      // is fine — the load-more guard prevents runaway calls.
      const entry = {
        isIntersecting: true,
        target,
        intersectionRatio: 1,
        boundingClientRect: target.getBoundingClientRect(),
        intersectionRect: target.getBoundingClientRect(),
        rootBounds: null,
        time: 0,
      } as unknown as IntersectionObserverEntry
      this.cb([entry], this as unknown as IntersectionObserver)
    }
    unobserve() {}
    disconnect() {}
    takeRecords(): IntersectionObserverEntry[] { return [] }
  }
  ;(window as unknown as { IntersectionObserver: unknown }).IntersectionObserver = StubIntersectionObserver
  ;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = StubIntersectionObserver
}

// jsdom polyfill: HTMLCanvasElement.getContext (used by scene canvases)
// jsdom doesn't implement canvas — mock getContext to return a no-op 2d context
const _origGetContext = HTMLCanvasElement.prototype.getContext
HTMLCanvasElement.prototype.getContext = function (type: string) {
  if (type === '2d') {
    const noop = () => {}
    const store: Record<string, any> = {}
    return new Proxy(store, {
      get: (_t, p) => (p in store ? store[p] : noop),
      set: (_t, p, v) => { store[p as string] = v; return true },
    }) as any
  }
  return _origGetContext.call(this, type) as any
}

// Start MSW server before all tests
beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))

// Reset handlers after each test to prevent test pollution
afterEach(() => server.resetHandlers())

// Remove the session-expired banner api/client.ts appends to document.body on a
// 403 — RTL doesn't clean up body-appended nodes, so it lingers and steals focus
// from later tests. Don't click its dismiss button: that resets the module guard
// and the banner resurfaces mid-userEvent.type elsewhere (broke CronTab).
afterEach(() => {
  document.getElementById('mc-session-expired')?.remove()
})

// Clean up after all tests are done
afterAll(() => server.close())
