import { useEffect, useRef } from 'react'
import anime from '../lib/anime.es'

/**
 * Kiro boot reveal. Narrative sequence on a Kiro-purple screen:
 *   1. A white Kiro ghost peeks in from the left edge (head tilted to look
 *      around the corner), then withdraws.
 *   2. It floats left -> right at a constant speed. A white panel trails it
 *      (lightly feathered edge). As the ghost passes each KIROCLAW character,
 *      that character's outline strokes itself on (stroke-dashoffset) and then
 *      fills with the Kiro purple, one letter after another.
 *   3. Ghost exits right, the wordmark stands complete, then the overlay
 *      dissolves into the dashboard.
 *
 * Decorative (aria-hidden, pointer-events: none). Respects prefers-reduced-motion.
 * anime.js drives the timeline (vendored at src/lib/anime.es.js for the prototype).
 */

const WORD = 'KIROCLAW'
const KIRO_PURPLE = '#9046FF' // Kiro brand purple (the app-icon background)
const KIRO_RGB = '144,70,255' // KIRO_PURPLE as rgb for animating fill alpha
const GHOST_W = 200
const FADE = 70   // px width of the soft sweep edge (writing lands just behind it)
const VB_W = 900  // wordmark SVG viewBox width
const VB_H = 160
const FONT_SIZE = 84
const STROKE_W = 3
const CHAR_SPACING = 8
const OUTLINE_MULT = 7 // dash length = advance width * this (>= glyph outline perimeter, so the outline completes)
const FLOAT_MS = 2400
const DRAW_MS = 320      // how long a single character's outline takes to draw
const FILL_MS = 260      // how long the fill fades in after the outline completes

interface Props {
  onDone: () => void
}

export default function BootReveal({ onDone }: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  const ghostRef = useRef<HTMLDivElement>(null)
  const whiteRef = useRef<HTMLDivElement>(null)
  const finishedRef = useRef(false)
  const doneRef = useRef(onDone)
  doneRef.current = onDone

  useEffect(() => {
    const root = rootRef.current
    const ghost = ghostRef.current
    const white = whiteRef.current
    const finish = () => {
      if (finishedRef.current) return
      finishedRef.current = true
      doneRef.current()
    }
    if (!root || !ghost || !white) { finish(); return }

    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n))
    const setGhost = (gx: number, bob = 0, rot = 0) => {
      ghost.style.transform = `translate(${gx}px, -50%) translateY(${bob}px) rotate(${rot}deg)`
    }

    const svg = root.querySelector('.boot-word') as SVGSVGElement | null
    // Lay out characters left-to-right (centered), prime each outline hidden.
    const charEls = Array.from(root.querySelectorAll<SVGTextElement>('.boot-char'))
    const widths = charEls.map(c => c.getComputedTextLength() || 50)
    const total = widths.reduce((a, b) => a + b, 0) + CHAR_SPACING * (charEls.length - 1)
    let cur = (VB_W - total) / 2
    const chars = charEls.map((c, i) => {
      c.setAttribute('x', String(cur))
      const vbLeft = cur
      cur += widths[i] + CHAR_SPACING
      const len = widths[i] * OUTLINE_MULT
      c.style.strokeDasharray = String(len)
      c.style.strokeDashoffset = String(len)
      return { el: c, len, vbLeft, screenLeft: 0, triggerMp: -1 }
    })

    if (reduce) {
      white.style.maskImage = 'none'
      white.style.setProperty('-webkit-mask-image', 'none')
      white.style.filter = 'none'
      white.style.width = '100%'
      ghost.style.opacity = '0'
      chars.forEach(c => { c.el.style.strokeDashoffset = '0'; c.el.style.fill = KIRO_PURPLE; c.el.style.stroke = 'transparent' })
      anime({ targets: root, opacity: [1, 0], duration: 380, delay: 900, easing: 'easeOutQuad', complete: finish })
      return () => { anime.remove(root) }
    }

    const HIDDEN_X = -GHOST_W - 30
    const PEEK_TILT = 16
    const PEEK_X = -GHOST_W + 130
    setGhost(HIDDEN_X)

    const prog = { p: 0 }
    const writeMp = { p: 0 }
    const DRAW_FRAC = DRAW_MS / FLOAT_MS
    const FILL_FRAC = FILL_MS / FLOAT_MS
    let startX = 0
    let endX = 0

    const tl = anime.timeline({ complete: finish })

    // 1. Peek in, head tilting right to look around the edge.
    tl.add({
      targets: prog, p: [0, 1], duration: 560, easing: 'easeOutBack',
      update: () => setGhost(HIDDEN_X + (PEEK_X - HIDDEN_X) * prog.p, Math.sin(prog.p * Math.PI) * -6, PEEK_TILT * prog.p),
    })
    // 2. Curious look-around while peeking.
    tl.add({
      targets: prog, p: [0, 1], duration: 460, easing: 'easeInOutSine',
      update: () => setGhost(PEEK_X, Math.sin(prog.p * Math.PI * 2) * 7, PEEK_TILT + Math.sin(prog.p * Math.PI * 2) * 3),
    })
    // 3. Withdraw, straightening up.
    tl.add({
      targets: prog, p: [0, 1], duration: 380, easing: 'easeInBack',
      update: () => setGhost(PEEK_X + (HIDDEN_X - PEEK_X) * prog.p, 0, PEEK_TILT * (1 - prog.p)),
    })
    // 4. Constant-speed float; each char strokes its outline when the ghost
    //    passes it, then fills in.
    tl.add({
      targets: writeMp, p: [0, 1], duration: FLOAT_MS, easing: 'linear',
      begin: () => {
        const r = svg?.getBoundingClientRect()
        const rl = r && r.width ? r.left : 0
        const rw = r && r.width ? r.width : 1
        chars.forEach(c => { c.screenLeft = rl + (c.vbLeft / VB_W) * rw })
        startX = -GHOST_W - FADE - 20
        endX = window.innerWidth + GHOST_W + FADE + 40
      },
      update: () => {
        const mp = writeMp.p
        const gx = startX + (endX - startX) * mp
        setGhost(gx, Math.sin(mp * Math.PI * 3) * 14)
        const front = gx + 30
        white.style.width = `${clamp(front, 0, endX + FADE + 60)}px`
        const solidEdge = front - FADE
        for (const c of chars) {
          if (c.triggerMp < 0 && solidEdge >= c.screenLeft) c.triggerMp = mp
          if (c.triggerMp >= 0) {
            const dp = clamp((mp - c.triggerMp) / DRAW_FRAC, 0, 1)
            c.el.style.strokeDashoffset = String(c.len * (1 - dp))
            const fp = clamp((mp - c.triggerMp - DRAW_FRAC) / FILL_FRAC, 0, 1)
            c.el.style.fill = `rgba(${KIRO_RGB},${fp})`
            c.el.style.stroke = `rgba(${KIRO_RGB},${1 - fp})` // fade outline out as fill arrives -> clean filled glyph
          }
        }
      },
    }, '+=120')
    // 5. Hold on the finished wordmark, then dissolve into the dashboard.
    tl.add({ targets: root, opacity: [1, 0], duration: 560, easing: 'easeInOutQuad' }, '+=420')

    return () => { anime.remove(prog); anime.remove(writeMp); anime.remove(root) }
  }, [])

  return (
    <div
      ref={rootRef}
      aria-hidden="true"
      className="fixed inset-0 z-[9999] overflow-hidden"
      style={{ background: KIRO_PURPLE, pointerEvents: 'none', willChange: 'opacity' }}
    >
      {/* White panel grows from the left with a lightly feathered right edge. */}
      <div
        ref={whiteRef}
        className="absolute top-0 left-0 h-full"
        style={{
          width: 0,
          background: '#ffffff',
          zIndex: 10,
          willChange: 'width',
          filter: 'blur(1.5px)',
          WebkitMaskImage: `linear-gradient(to right, #000 calc(100% - ${FADE}px), transparent 100%)`,
          maskImage: `linear-gradient(to right, #000 calc(100% - ${FADE}px), transparent 100%)`,
        }}
      />

      {/* KIROCLAW — each character is its own text element; outlines stroke on
          then fill, one after another as the ghost passes. */}
      <div className="absolute inset-0 flex items-center justify-center" style={{ zIndex: 20 }}>
        <svg
          className="boot-word"
          width={VB_W}
          height={VB_H}
          viewBox={`0 0 ${VB_W} ${VB_H}`}
          style={{ maxWidth: '92vw', height: 'auto' }}
          aria-hidden="true"
        >
          {WORD.split('').map((ch, i) => (
            <text
              key={i}
              className="boot-char"
              x={0}
              y={VB_H / 2}
              textAnchor="start"
              dominantBaseline="central"
              fontSize={FONT_SIZE}
              fontWeight={800}
              fill="none"
              stroke={KIRO_PURPLE}
              strokeWidth={STROKE_W}
              strokeLinejoin="round"
              strokeLinecap="round"
              style={{ fontFamily: 'inherit' }}
            >
              {ch}
            </text>
          ))}
        </svg>
      </div>

      {/* Kiro ghost (white body, black eyes). Inlined rather than an <img>
          asset so it paints instantly on the boot splash -- loading it as an
          asset introduced a ~1s first-load delay before the ghost appeared. */}
      <div
        ref={ghostRef}
        className="absolute"
        style={{ top: '50%', left: 0, zIndex: 30, willChange: 'transform', filter: 'drop-shadow(0 10px 26px rgba(40,20,90,0.35))' }}
      >
        <svg width={GHOST_W} height={Math.round(GHOST_W * 980 / 745)} viewBox="255 110 745 980" fill="none" aria-hidden="true">
          <path d="M398.554 818.914C316.315 1001.03 491.477 1046.74 620.672 940.156C658.687 1059.66 801.052 970.473 852.234 877.795C964.787 673.567 919.318 465.357 907.64 422.374C827.637 129.443 427.623 128.946 358.8 423.865C342.651 475.544 342.402 534.18 333.458 595.051C328.986 625.86 325.507 645.488 313.83 677.785C306.873 696.424 297.68 712.819 282.773 740.645C259.915 783.881 269.604 867.113 387.87 823.883L399.051 818.914H398.554Z" fill="#ffffff" />
          <path d="M636.123 549.353C603.328 549.353 598.359 510.097 598.359 486.742C598.359 465.623 602.086 448.977 609.293 438.293C615.504 428.852 624.697 424.131 636.123 424.131C647.555 424.131 657.492 428.852 664.447 438.541C672.398 449.474 676.623 466.12 676.623 486.742C676.623 525.998 661.471 549.353 636.375 549.353H636.123Z" fill="#000000" />
          <path d="M771.24 549.353C738.445 549.353 733.477 510.097 733.477 486.742C733.477 465.623 737.203 448.977 744.41 438.293C750.621 428.852 759.814 424.131 771.24 424.131C782.672 424.131 792.609 428.852 799.564 438.541C807.516 449.474 811.74 466.12 811.74 486.742C811.74 525.998 796.588 549.353 771.492 549.353H771.24Z" fill="#000000" />
        </svg>
      </div>
    </div>
  )
}
