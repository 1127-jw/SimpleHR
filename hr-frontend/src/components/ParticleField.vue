<script setup lang="ts">
/**
 * 粒子背景（Canvas 2D）
 *
 * 三层效果：
 *  1. 粒子场：全屏分布的细粒子，叠加在网格上；
 *  2. 鼠标交互：光标附近的粒子被推开，随后由弹簧力缓缓回到原位，被推动时也会轻微拖拽相邻粒子；
 *  3. 文字粒子：品牌字样由粒子拼出——先渲染文字到位图、再按像素采样得到目标点，
 *     粒子用弹簧力飞向这些点；鼠标划过时把它们打散，松开后又重新聚合。
 *
 * 性能与体验：
 *  - devicePixelRatio 上限 2，避免高 DPI 屏粒子过多；
 *  - 文字采样按像素步长抽稀，控制粒子总量（上限 2600）；
 *  - 单路径批量绘制 + 每个粒子一个渐变，避免逐粒子 fill 造成大量状态切换；
 *  - 页面不可见时暂停动画，标签页恢复后继续（省电）；
 *  - 尊重 prefers-reduced-motion：静态渲染一帧，不启动动画循环。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { PropType } from 'vue'

interface Particle {
  x: number
  y: number
  vx: number
  vy: number
  hx: number
  hy: number
  size: number
  alpha: number
  bright: number
  fade: number
  stiffness: number
  damping: number
  text: boolean
  glow: boolean
  glowColor: string
}

type Point = [number, number]

const props = defineProps({
  /** 文字内容；传空字符串则只渲染背景粒子场 */
  text: { type: String, default: 'SimpleHR' },
  /** 粒子基调色（RGB 数组），文字粒子会在此基础上更亮 */
  color: { type: Array as PropType<number[]>, default: () => [122, 152, 255] },
  /** 文字位置：center（登录页居中）| upper（工作台偏上，避开顶栏与侧边栏） */
  variant: { type: String, default: 'upper' },
  /** 文字粒子的垂直中心（画布高度占比）；null 则按 variant 取默认值 */
  positionY: { type: Number, default: null },
  /** 文字粒子顶端的最小 y（像素），用于避开上方元素 */
  minTop: { type: Number, default: 0 },
  /** 文字粒子底端的最大 y（像素），用于避开下方元素 */
  maxBottom: { type: Number, default: 0 },
  /** 文字字号（固定像素，不随窗口缩放；窗口太窄时才按比例缩小以免裁切） */
  fontSize: { type: Number, default: 148 },
})

const canvasRef = ref<HTMLCanvasElement | null>(null)

// ---------- 可调参数 ----------
const MAX_DPR = 2
const BASE_PARTICLES_PER_MEGAPIXEL = 5600 // 背景粒子密度（每百万 CSS 像素）
const MAX_BASE_PARTICLES = 900
const MAX_TEXT_PARTICLES = 2600
const MOUSE_RADIUS_FACTOR = 0.145 // 鼠标影响半径 = min(w,h) * 该系数
const MIN_MOUSE_RADIUS = 90
const MAX_MOUSE_RADIUS = 210
const GLOW_RADIUS = 2.2 // 文字粒子辉光半径（× 粒子尺寸）；越小越省，观感几乎不变

let ctx: CanvasRenderingContext2D | null = null
let raf = 0
let running = false
let visibilityHandler: (() => void) | null = null
let resizeObserver: ResizeObserver | null = null
let resizeTimer = 0

let width = 0
let height = 0
let mouse = { x: -9999, y: -9999, active: false }
let particles: Particle[] = []
let mouseRadius = 150
let mouseRadiusSq = 22500
// 是否首次构建：首帧粒子从随机位置飞入形成入场动画；之后（窗口缩放重建）直接落到目标点
let isInitialBuild = true

function rand(min: number, max: number) {
  return min + Math.random() * (max - min)
}

/** 生成「文字 → 采样点」：把文字画到离屏画布，再按像素采样出粒子目标坐标。 */
function sampleTextPoints(text: string, w: number, h: number): Point[] {
  const off = document.createElement('canvas')
  off.width = Math.max(1, Math.floor(w))
  off.height = Math.max(1, Math.floor(h))
  const octx = off.getContext('2d')
  if (!octx) return []

  // 1. 字号固定，不随浏览器窗口缩放；唯一例外：窗口窄到整行放不下时才等比缩小，
  //    避免文字被裁掉。
  const fontFamily =
    "'Inter', 'HarmonyOS Sans SC', 'Segoe UI', system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif"
  const baseSize = props.fontSize
  octx.font = `700 ${baseSize}px ${fontFamily}`
  const baseWidth = octx.measureText(text).width || 1
  const maxWidth = w * 0.94
  const fontSize = baseWidth > maxWidth ? Math.max(20, baseSize * (maxWidth / baseWidth)) : baseSize

  // 2. 垂直位置：先按 positionY 比例算出中心，再用页面实测的上下边界夹取。
  const rawY = props.positionY ?? (props.variant === 'center' ? 0.47 : 0.46)
  const ratioY = Math.min(0.85, Math.max(0.15, rawY)) * h
  const half = fontSize * 0.36
  const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)
  let yCenter = ratioY
  const upperLimit = props.minTop > 0 ? props.minTop + half : half + 4
  const lowerLimit = props.maxBottom > 0 ? props.maxBottom - half : Math.min(ratioY, h - half - 4)
  yCenter = clamp(yCenter, upperLimit, Math.max(lowerLimit, upperLimit))
  yCenter = Math.min(Math.max(yCenter, half + 4), h - half - 4)
  octx.clearRect(0, 0, w, h)
  octx.font = `700 ${fontSize}px ${fontFamily}`
  octx.textAlign = 'center'
  octx.textBaseline = 'middle'
  octx.fillStyle = '#fff'
  octx.fillText(text, w / 2, yCenter)

  // 3. 像素采样：读 alpha 通道取字形内部像素。
  const data = octx.getImageData(0, 0, off.width, off.height).data
  const alphaAt = (x: number, y: number) => data[(y * off.width + x) * 4 + 3] ?? 0
  const isSmall = w < 640
  const targetPoints = isSmall ? 800 : 2100
  const minPoints = isSmall ? 450 : 1100
  const minStep = isSmall ? 4 : 5

  const sampleWithStep = (step: number): Point[] => {
    const points: Point[] = []
    for (let y = 0; y < off.height; y += step) {
      for (let x = 0; x < off.width; x += step) {
        if (alphaAt(x, y) > 128) {
          points.push([x + rand(-1.2, 1.2), y + rand(-1.2, 1.2)])
        }
      }
    }
    return points
  }

  // 用最小步长抽样测量真实字形覆盖率（只取画布 1/16 面积的格子，开销可忽略），
  // 再用它反解「点数 ≈ 目标」所需的步长。
  let hits = 0
  let tested = 0
  const probe = minStep * 4
  for (let y = 0; y < off.height; y += probe) {
    for (let x = 0; x < off.width; x += probe) {
      tested++
      if (alphaAt(x, y) > 128) hits++
    }
  }
  const coverage = tested ? hits / tested : 0.1

  let step = minStep
  if (coverage > 0) {
    const needed = Math.ceil(Math.sqrt((coverage * w * h) / targetPoints))
    step = Math.max(minStep, needed)
  }

  let points = sampleWithStep(step)
  if (points.length > MAX_TEXT_PARTICLES) {
    step = Math.max(step + 1, Math.ceil(step * Math.sqrt(points.length / MAX_TEXT_PARTICLES)))
    points = sampleWithStep(step)
  } else {
    for (let s = step - 1; s >= 2 && points.length < minPoints; s--) {
      points = sampleWithStep(s)
    }
  }
  if (points.length > MAX_TEXT_PARTICLES) points = points.slice(0, MAX_TEXT_PARTICLES)

  // 打乱，便于超量时均匀抽稀（而不是只保留文字左半边）
  for (let i = points.length - 1; i > 0; i--) {
    const j = (Math.random() * (i + 1)) | 0
    const tmp = points[i]!
    points[i] = points[j]!
    points[j] = tmp
  }
  return points
}

function buildParticles() {
  const cssW = width
  const cssH = height
  if (!cssW || !cssH) return

  const isText = !!props.text
  // 文字模式下背景粒子更少、更暗，避免和中央字样抢视线
  const density = isText ? BASE_PARTICLES_PER_MEGAPIXEL * 0.42 : BASE_PARTICLES_PER_MEGAPIXEL
  const baseCount = Math.min(MAX_BASE_PARTICLES, Math.round(((cssW * cssH) / 1e6) * density))

  mouseRadius = Math.min(
    MAX_MOUSE_RADIUS,
    Math.max(MIN_MOUSE_RADIUS, Math.min(cssW, cssH) * MOUSE_RADIUS_FACTOR),
  )
  mouseRadiusSq = mouseRadius * mouseRadius

  const next: Particle[] = []

  // ---- 1. 背景粒子场 ----
  for (let i = 0; i < baseCount; i++) {
    const x = Math.random() * cssW
    const y = Math.random() * cssH
    next.push({
      x,
      y,
      vx: 0,
      vy: 0,
      hx: x,
      hy: y,
      size: rand(0.7, 1.7),
      alpha: rand(0.1, 0.34),
      bright: 1,
      fade: 1,
      stiffness: rand(0.004, 0.012),
      damping: 0.9,
      text: false,
      glow: false,
      glowColor: '',
    })
  }

  // ---- 2. 文字粒子 ----
  if (isText) {
    let points = sampleTextPoints(props.text, cssW, cssH)
    if (points.length > MAX_TEXT_PARTICLES) {
      points = points.slice(0, MAX_TEXT_PARTICLES)
    }
    const lift = 0.5 // 文字粒子亮度系数，配合辉光形成视觉焦点
    for (const [px, py] of points) {
      const scatter = isInitialBuild
      next.push({
        x: scatter ? rand(0, cssW) : px,
        y: scatter ? rand(0, cssH) : py,
        vx: scatter ? rand(-0.4, 0.4) : 0,
        vy: scatter ? rand(-0.4, 0.4) : 0,
        hx: px,
        hy: py,
        size: rand(1.1, 2.0),
        alpha: rand(0.7, 1),
        bright: lift + 0.55,
        fade: 1,
        stiffness: rand(0.02, 0.042),
        damping: 0.86,
        text: true,
        glow: true,
        glowColor: 'rgba(122,152,255,0)',
      })
    }
  }

  particles = next
}

/** 重新采样文字点位并把「文字粒子」的目标位置迁移过去（不重建整场，粒子会平滑飞过去）。 */
function retargetTextParticles() {
  if (!props.text || !width || !height) return
  let points = sampleTextPoints(props.text, width, height)
  if (points.length > MAX_TEXT_PARTICLES) points = points.slice(0, MAX_TEXT_PARTICLES)

  const textParticles = particles.filter((p) => p.text)
  if (!points.length || !textParticles.length) return

  for (let i = points.length - 1; i > 0; i--) {
    const j = (Math.random() * (i + 1)) | 0
    const tmp = points[i]!
    points[i] = points[j]!
    points[j] = tmp
  }
  textParticles.forEach((p, i) => {
    const pt = i < points.length ? points[i] : points[points.length - 1 - (i % 24)]
    if (!pt) return
    p.hx = pt[0]
    p.hy = pt[1]
  })
}

function resize() {
  const canvas = canvasRef.value
  if (!canvas) return
  const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR)
  const cssW = canvas.clientWidth || window.innerWidth
  const cssH = canvas.clientHeight || window.innerHeight

  canvas.width = Math.max(1, Math.floor(cssW * dpr))
  canvas.height = Math.max(1, Math.floor(cssH * dpr))
  ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0) // 之后一律用 CSS 像素坐标绘制

  width = cssW
  height = cssH
  buildParticles()
  isInitialBuild = false
}

function onPointerMove(e: PointerEvent) {
  const canvas = canvasRef.value
  if (!canvas) return
  const rect = canvas.getBoundingClientRect()
  mouse.x = e.clientX - rect.left
  mouse.y = e.clientY - rect.top
  mouse.active = true
}

function onPointerLeave() {
  mouse.active = false
  mouse.x = -9999
  mouse.y = -9999
}

const [cr = 122, cg = 152, cb = 255] = props.color

function draw() {
  if (!ctx) return
  ctx.clearRect(0, 0, width, height)

  // 第一遍：给「文字粒子」铺一层低透明度的辉光，让字样在标题文案之上依然醒目。
  // 每帧都绘制，强度取恒定值，避免弹簧振荡导致逐帧明暗跳变。
  for (const p of particles) {
    if (!p.glow) continue
    ctx.fillStyle = p.glowColor
    const r = p.size * GLOW_RADIUS
    ctx.fillRect(p.x - r, p.y - r, r * 2, r * 2)
  }

  // 第二遍：物理更新 + 实心点。
  for (const p of particles) {
    // ---- 鼠标斥力 ----
    if (mouse.active) {
      const dx = p.x - mouse.x
      const dy = p.y - mouse.y
      const d2 = dx * dx + dy * dy
      if (d2 < mouseRadiusSq && d2 > 0.01) {
        const d = Math.sqrt(d2)
        const t = 1 - d / mouseRadius // 越近推力越大
        const force = t * t * 3.2
        p.vx += (dx / d) * force
        p.vy += (dy / d) * force
      }
    }

    // ---- 弹簧回归原位 ----
    p.vx += (p.hx - p.x) * p.stiffness
    p.vy += (p.hy - p.y) * p.stiffness
    p.vx *= p.damping
    p.vy *= p.damping
    p.x += p.vx
    p.y += p.vy

    // ---- 亮度：离原位越远越暗（被搅动处形成涟漪）----
    // 对位移做时间缓动（EMA），避免弹簧过冲导致逐帧明暗跳变。
    const dx0 = p.x - p.hx
    const dy0 = p.y - p.hy
    const target = 1 - Math.min(1, Math.sqrt(dx0 * dx0 + dy0 * dy0) / 140) * 0.75 // 映射到 [0.25, 1]
    p.fade += (target - p.fade) * 0.08

    let alpha: number
    if (p.text) {
      alpha = Math.min(1, p.alpha * p.fade * p.bright)
      const mix = 0.62 * p.bright * p.fade
      const r = Math.round(Math.min(255, cr + (250 - cr) * mix))
      const g = Math.round(Math.min(255, cg + (252 - cg) * mix))
      const b = Math.round(Math.min(255, cb + (255 - cb) * mix))
      ctx.fillStyle = `rgba(${r},${g},${b},${alpha.toFixed(3)})`
      p.glowColor = `rgba(${r},${g},${b},${(Math.min(1, p.alpha * p.bright) * 0.28).toFixed(3)})`
    } else {
      alpha = Math.min(1, p.alpha * p.fade)
      ctx.fillStyle = `rgba(${cr},${cg},${cb},${alpha.toFixed(3)})`
    }

    const s = p.size
    ctx.fillRect(p.x - s, p.y - s, s * 2, s * 2)
  }
}

function loop() {
  if (!running) return
  draw()
  raf = requestAnimationFrame(loop)
}

function start() {
  if (running) return
  running = true
  raf = requestAnimationFrame(loop)
}

function stop() {
  running = false
  if (raf) cancelAnimationFrame(raf)
  raf = 0
}

onMounted(async () => {
  // 等字体就绪后再采样文字，避免用回退字体取点导致字形跳动
  if (document.fonts && document.fonts.ready) {
    try {
      await document.fonts.ready
    } catch (_) {
      /* 忽略：字体 API 不可用时直接采样 */
    }
  }
  resize()

  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (prefersReduced) {
    draw() // 静态渲染一帧，不做动画
    return
  }

  window.addEventListener('pointermove', onPointerMove, { passive: true })
  window.addEventListener('pointerdown', onPointerMove, { passive: true })
  window.addEventListener('pointerleave', onPointerLeave)

  // 尺寸变化：防抖重建（重建会重置粒子位置）
  resizeObserver = new ResizeObserver(() => {
    clearTimeout(resizeTimer)
    resizeTimer = window.setTimeout(resize, 160)
  })
  if (canvasRef.value) resizeObserver.observe(canvasRef.value)

  // 页面不可见时暂停，省电
  visibilityHandler = () => (document.hidden ? stop() : start())
  document.addEventListener('visibilitychange', visibilityHandler)

  start()
})

onBeforeUnmount(() => {
  stop()
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerdown', onPointerMove)
  window.removeEventListener('pointerleave', onPointerLeave)
  if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler)
  if (resizeObserver) resizeObserver.disconnect()
  clearTimeout(resizeTimer)
})

// 上下避让位置变化时，把文字粒子重新定位过去。
watch(
  () => [props.minTop, props.maxBottom],
  () => retargetTextParticles(),
)
</script>

<template>
  <canvas ref="canvasRef" class="particle-field" aria-hidden="true"></canvas>
</template>

<style scoped>
/* 固定满屏、不拦截鼠标事件。
   层级安排：背景层（.app-bg / .bg）z-index:0 → 粒子 z-index:1 → 内容 z-index:2，
   这样粒子浮在网格与光晕之上、卡片与文字之下。 */
.particle-field {
  position: fixed;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  pointer-events: none;
  z-index: 1;
}
</style>
