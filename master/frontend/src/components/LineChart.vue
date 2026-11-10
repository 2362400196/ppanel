<script setup>
// 自研 SVG 折线图（无第三方依赖）：多序列 + 网格 + 图例，用于节点监控曲线
import { computed } from 'vue'

const props = defineProps({
  points: { type: Array, default: () => [] },   // [{ ts, <key>: number, ... }]
  series: { type: Array, default: () => [] },   // [{ key, label, color, max? }]
  height: { type: Number, default: 150 },
  span: { type: String, default: '24h' }        // 24h → HH:mm；7d → MM-DD HH:mm
})

const W = 640
const PAD = { l: 42, r: 10, t: 10, b: 20 }
const H = computed(() => props.height)
const innerW = W - PAD.l - PAD.r
const innerH = computed(() => H.value - PAD.t - PAD.b)

function xPos(i) {
  const n = props.points.length
  return PAD.l + (n <= 1 ? innerW : innerW * i / (n - 1))
}
function yPos(v, max) {
  return PAD.t + innerH.value * (1 - Math.min(Math.max(v, 0), max) / (max || 1))
}

const perSeries = computed(() => props.series.map(s => {
  const vals = props.points.map(p => Number(p[s.key]) || 0)
  const maxV = s.max || Math.max(1, ...vals, 0) * 1.15
  return {
    ...s,
    maxV: Math.ceil(maxV * 100) / 100,
    d: props.points.map((p, i) => `${xPos(i).toFixed(1)},${yPos(vals[i], maxV).toFixed(1)}`).join(' '),
    area: props.points.length > 1
      ? `${PAD.l},${PAD.t + innerH.value} ${props.points.map((p, i) =>
        `${xPos(i).toFixed(1)},${yPos(vals[i], maxV).toFixed(1)}`).join(' ')} ${xPos(props.points.length - 1)},${PAD.t + innerH.value}`
      : '',
    last: vals.length ? vals[vals.length - 1] : null
  }
}))

const grid = computed(() => [1, .75, .5, .25, 0].map(r => ({
  y: PAD.t + innerH.value * (1 - r),
  label: Math.round((perSeries.value[0]?.maxV || 1) * r * 10) / 10
})))

const timeLabels = computed(() => {
  const pts = props.points
  if (!pts.length) return []
  const idx = [0, Math.floor((pts.length - 1) / 2), pts.length - 1]
  const seen = new Set()
  const out = []
  for (const i of idx) {
    if (seen.has(i)) continue
    seen.add(i)
    const raw = pts[i].ts
    const d = new Date(/[zZ+]/.test(raw) ? raw : raw + 'Z')  // 被控存 UTC 无时区标记
    if (isNaN(d)) continue
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    out.push({ x: xPos(i), text: props.span === '7d' ? `${mm}-${dd} ${hm}` : hm })
  }
  return out
})
</script>

<template>
  <div class="lc">
    <div class="lc-legend">
      <span v-for="s in perSeries" :key="s.key" class="lc-item">
        <i :style="{ background: s.color }" />{{ s.label }}
        <b class="mono" v-if="s.last != null">{{ s.last }}</b>
      </span>
    </div>
    <svg :viewBox="`0 0 ${W} ${height}`" :style="{ height: height + 'px' }">
      <g v-for="(g, i) in grid" :key="i" class="lc-grid">
        <line :x1="PAD.l" :x2="W - PAD.r" :y1="g.y" :y2="g.y" />
        <text :x="PAD.l - 6" :y="g.y + 3">{{ g.label }}</text>
      </g>
      <template v-if="points.length > 1">
        <polygon v-for="s in perSeries" :key="'a' + s.key" :points="s.area"
                 :fill="s.color" class="lc-area" />
        <polyline v-for="s in perSeries" :key="s.key" :points="s.d"
                  fill="none" :stroke="s.color" class="lc-line" />
      </template>
      <text v-for="(t, i) in timeLabels" :key="'t' + i" class="lc-tlabel"
            :x="t.x" :y="height - 4" :text-anchor="i === 0 ? 'start' : (t.x > W / 2 ? 'end' : 'middle')">
        {{ t.text }}
      </text>
      <text v-if="!points.length" :x="W / 2" :y="height / 2" text-anchor="middle" class="lc-empty">
        暂无采样数据（节点运行约 1 分钟后开始出现曲线）
      </text>
    </svg>
  </div>
</template>

<style scoped>
.lc { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 10px 10px 4px; }
.lc-legend { display: flex; gap: 16px; padding: 0 4px 6px; }
.lc-item { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-dim); }
.lc-item i { width: 10px; height: 3px; border-radius: 2px; display: inline-block; }
.lc-item b { color: var(--text); font-weight: 600; }
svg { width: 100%; display: block; }
.lc-grid line { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 3; }
.lc-grid text { font-size: 10px; fill: var(--text-dim); text-anchor: end; }
.lc-line { stroke-width: 1.8; stroke-linejoin: round; stroke-linecap: round; }
.lc-area { opacity: .08; }
.lc-tlabel { font-size: 10px; fill: var(--text-dim); }
.lc-empty { font-size: 12px; fill: var(--text-dim); }
</style>
