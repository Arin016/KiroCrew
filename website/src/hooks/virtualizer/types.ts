// Public types for the chat virtualizer hook.

export interface UseVirtualChatOptions<T> {
  /** Full list of items in display order. */
  items: T[]
  /** Stable key extractor used as the height-cache key. */
  getKey: (item: T, index: number) => string
  /** Height to use when no measurement is cached. Default: 80. */
  estimatedHeight?: number
  /** Items to mount above and below the visible viewport. Default: 5. */
  overscan?: number
  /** Session ID — partitions the persisted height cache. */
  sessionId: string
  /**
   * Whether to pin the scroll position to the bottom on item appends.
   * Default: true. The user scrolling away from the bottom always disables
   * pinning regardless of this option (see Property 7).
   */
  followOutput?: boolean
  /**
   * Threshold in pixels from the bottom below which `isAtBottom` becomes
   * true. Default: 100. The same threshold gates the follow-output
   * auto-pin so callers can tune sensitivity.
   */
  bottomThreshold?: number
  /**
   * Optional predicate: items for which this returns `true` are mounted
   * regardless of the viewport window — they never become placeholders.
   * Useful for items with expensive children (widget iframes, images
   * with custom decoders) that lose state on unmount and recreate it
   * slowly on remount, causing visible flicker.
   *
   * Use sparingly: each sticky item permanently consumes the memory and
   * resources of its mounted form for the session lifetime.
   */
  isSticky?: (item: T, index: number) => boolean
  /**
   * Optional external scroll container ref. When provided, the hook
   * manages the same DOM element instead of creating a new one. Useful
   * when integrating with existing scroll-management code that owns the
   * scroller. Accepts both `RefObject<HTMLDivElement>` (non-null) and
   * `RefObject<HTMLDivElement | null>` (nullable) styles of useRef result.
   */
  externalScrollerRef?: React.RefObject<HTMLDivElement | null> | React.RefObject<HTMLDivElement>
}

export interface VirtualItem<T> {
  /** Original item data — render this when `mounted` is true. */
  data: T
  /** Index in the source array. */
  index: number
  /** Stable key (from getKey) used for React reconciliation and cache. */
  key: string
  /** True when the item should render as a full React component. */
  mounted: boolean
  /** Measured (or estimated) height — placeholder size when !mounted. */
  height: number
}

export interface ScrollToIndexOptions {
  align?: 'start' | 'center' | 'end'
  behavior?: ScrollBehavior
}

export interface UseVirtualChatReturn<T> {
  /** Attach to the scroll container (`overflow-y: auto`). */
  scrollerRef: React.RefObject<HTMLDivElement | null>
  /** Attach to the inner content wrapper (sized to totalHeight). */
  contentRef: React.RefObject<HTMLDivElement>
  /** Top sentinel — attach for upward expansion detection. */
  topSentinelRef: React.RefObject<HTMLDivElement>
  /** Bottom sentinel — attach for downward expansion detection. */
  bottomSentinelRef: React.RefObject<HTMLDivElement>
  /** Items to render — both mounted React components and placeholder rows. */
  virtualItems: VirtualItem<T>[]
  /** Pixel offset of the first virtual item (top spacer height). */
  offsetBefore: number
  /** Pixel height of all items after the window (bottom spacer height). */
  offsetAfter: number
  /** Total content height (top spacer + window + bottom spacer). */
  totalHeight: number
  /** Whether the scroller is at (within bottomThreshold of) the bottom. */
  isAtBottom: boolean
  /** Scroll the scroller so item `index` is visible. */
  scrollToIndex: (index: number, opts?: ScrollToIndexOptions) => void
  /** "Human-like" smooth scroll to `index` without pre-mounting a window —
   * animates scrollTop and lets the scroll listener mount rows progressively,
   * keeping the window tight (avoids a wide always-mounted span). */
  scrollToIndexSmooth: (index: number, opts?: { align?: 'start' | 'center'; offset?: number }) => void
  /** Scroll to the bottom (latest message). */
  scrollToBottom: (behavior?: ScrollBehavior) => void
  /** Ensure `index` is mounted (in the window) without scrolling — lets a
   * caller's DOM-based scroll target an off-window item. Returns `true` when
   * it took the FAR path (window replaced, leaving an unmounted gap to the
   * target) so callers can teleport instead of gliding through blank space. */
  mountIndex: (index: number) => boolean
  /** Ref callback used per-item to register ResizeObserver measurement. */
  measureRef: (index: number) => (el: HTMLElement | null) => void
}
