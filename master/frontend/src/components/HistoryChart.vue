<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

// 自研 SVG 历史曲线：多系列、渐变面积填充、悬停浮层
const props = defineProps({
  points: { type: Array, default: () => [] }, // [{ts, ...seriesKey}]
  series: { type: Array, required: true },    // [{key,label,color,unit,percent}]
  height: { type: Number, default: 170 }
})

const PAD = { l: 40, r: 12, t: 12, b: 22 }
const wrap = ref(null)
const width = ref(600)
const hoverIdx = ref(null)
let ro

onMounted(() => {
  ro = new ResizeObserver(es => {
    const w = es[0]?.contentRect?.width
    if (w) width.value = w
  })
  ro.observe(wrap.value)
})
onUnmounted(() => ro?.disconnect())

const innerW = computed(() => Math.max(10, width.value - PAD.l - PAD.r))
const innerH = computed(() => props.height - PAD.t - PAD.b)

const vals = computed(() => props.series.map(s => ({
  ...s,
  nums: props.points.map(p => Number(p[s.key]) || 0)
})))

function yMaxOf(s) {
  if (s.percent) return 100
  const mx = Math.max(...s.nums, 0)
  if (mx <= 0) return 1
  const step = mx > 2048 ? 1024 : mx > 512 ? 256 : mx > 64 ? 32 : mx > 8 ? 4 : 1
  return Math.max(1, Math.ceil((mx * 1.15) / step) * step)
}

function xAt(i) {
  const n = props.points.length
  if (n < 2) return PAD.l
  return PAD.l + (i / (n - 1)) * innerW.value
}

function yAt(v, yMax) {
  const ratio = Math.min(1, Math.max(0, v / yMax))
  return PAD.t + (1 - ratio) * innerH.value
}

const lines = computed(() => vals.value.map(s => {
  const yMax = yMaxOf(s)
  const pts = s.nums.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v, yMax).toFixed(1)}`)
  return {
    ...s, yMax,
    line: 'M' + pts.join(' L'),
    area: pts.length
      ? `M${pts.join(' L')} L${xAt(s.nums.length - 1).toFixed(1)},${PAD.t + innerH.value} L${PAD.l},${PAD.t + innerH.value} Z`
      : ''
  }
}))

const yTicks = computed(() => {
  const yMax = yMaxOf(vals.value[0] || {})
  return [0, 0.25, 0.5, 0.75, 1].map(f => ({
    y: PAD.t + (1 - f) * innerH.value,
    label: String(Math.round(yMax * f))
  }))
})

const xLabels = computed(() => {
  const n = props.points.length
  if (n < 2) return []
  const fmt = ts => {
    const d = new Date(ts)
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  }
  return [0, Math.floor((n - 1) / 2), n - 1].map(i => ({
    x: xAt(i), label: fmt(props.points[i]?.ts)
  }))
})

const hover = computed(() => {
  if (hoverIdx.value == null || !props.points.length) return null
  const i = hoverIdx.value
  const rows = vals.value.map(s => ({
    label: s.label, color: s.color, unit: s.unit || '',
    value: (s.nums[i] ?? 0).toFixed(s.percent ? 1 : 1)
  }))
  return { x: xAt(i), rows, ts: props.points[i]?.ts }
})

function onMove(e) {
  const rect = wrap.value.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const n = props.points.length
  if (n < 2) return
  const idx = Math.round(((mx - PAD.l) / innerW.value) * (n - 1))
  hoverIdx.value = Math.min(n - 1, Math.max(0, idx))
}

const tipStyle = computed(() => {
  if (!hover.value) return {}
  const flip = hover.value.x > width.value - 150
  return {
    left: `${hover.value.x + (flip ? -10 : 10)}px`,
    transform: flip ? 'translateX(-100%)' : 'none',
    top: '8px'
  }
})
</script>

<template>
  <div ref="wrap" class="hchart" :style="{ height: height + 'px' }" @mousemove="onMove" @mouseleave="hoverIdx = null">
    <svg v-if="points.length > 1" :width="width" :height="height" class="svg">
      <defs>
        <linearGradient v-for="s in lines" :key="s.key" :id="`g-${s.key}`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" :stop-color="s.color" stop-opacity="0.22" />
          <stop offset="100%" :stop-color="s.color" stop-opacity="0.02" />
        </linearGradient>
      </defs>

      <g v-for="t in yTicks" :key="t.y">
        <line :x1="PAD.l" :x2="width - PAD.r" :y1="t.y" :y2="t.y" class="grid" />
        <text :x="PAD.l - 6" :y="t.y + 3" class="tick" text-anchor="end">{{ t.label }}</text>
      </g>

      <template v-for="s in lines" :key="s.key">
        <path :d="s.area" :fill="`url(#g-${s.key})`" />
        <path :d="s.line" fill="none" :stroke="s.color" stroke-width="1.8"
              stroke-linejoin="round" stroke-linecap="round" />
      </template>

      <line v-if="hover" :x1="hover.x" :x2="hover.x" :y1="PAD.t" :y2="PAD.t + innerH" class="cursor" />
      <template v-for="l in xLabels" :key="l.label + l.x">
        <text :x="l.x" :y="height - 6" class="tick" text-anchor="middle">{{ l.label }}</text>
      </template>
    </svg>

    <div v-else class="empty">
      <span>采样数据累积中… 每 60 秒记录一个点</span>
    </div>

    <div v-if="hover" class="tip" :style="tipStyle">
      <div class="tip-time">{{ new Date(hover.ts).toLocaleTimeString() }}</div>
      <div v-for="r in hover.rows" :key="r.label" class="tip-row">
        <i class="sw" :style="{ background: r.color }" />{{ r.label }}
        <b>{{ r.value }}{{ r.unit }}</b>
      </div>
    </div>

    <div class="legend">
      <span v-for="s in series" :key="s.key" class="li">
        <i class="sw" :style="{ background: s.color }" />{{ s.label }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.hchart { position: relative; width: 100%; }
.svg { display: block; }
.grid { stroke: var(--line); stroke-width: 1; }
.tick { font-size: 10px; fill: var(--text-dim); font-family: inherit; }
.cursor { stroke: var(--primary); stroke-width: 1; stroke-dasharray: 3 3; opacity: .6; }
.empty {
  height: 100%; display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 12px;
}
.legend {
  position: absolute; top: 0; right: 0; display: flex; gap: 12px;
  font-size: 11px; color: var(--text-2); pointer-events: none;
}
.li { display: inline-flex; align-items: center; gap: 5px; }
.sw { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }

.tip {
  position: absolute; pointer-events: none; z-index: 2;
  background: var(--card-bg, #fff); border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 10px; box-shadow: var(--shadow-lg, 0 8px 24px rgba(0,0,0,.1));
  font-size: 11px; color: var(--text-2); min-width: 120px;
}
.tip-time { color: var(--text-dim); margin-bottom: 4px; }
.tip-row { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
.tip-row b { margin-left: auto; color: var(--text); font-weight: 600; }
</style>
