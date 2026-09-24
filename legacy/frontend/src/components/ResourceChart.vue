<script setup>
import { onMounted, ref, watch } from 'vue'

// 轻量资源曲线：双系列（CPU / 内存），canvas 绘制
const props = defineProps({
  cpuHistory: { type: Array, default: () => [] },
  memHistory: { type: Array, default: () => [] },
  memLimitMb: { type: Number, default: 0 },
  memUsageMb: { type: Number, default: 0 }
})

const canvas = ref(null)

function draw() {
  const c = canvas.value
  if (!c) return
  const dpr = window.devicePixelRatio || 1
  const w = c.clientWidth, h = c.clientHeight
  c.width = w * dpr
  c.height = h * dpr
  const ctx = c.getContext('2d')
  ctx.scale(dpr, dpr)
  ctx.clearRect(0, 0, w, h)

  // 网格
  ctx.strokeStyle = 'rgba(47,181,159,.1)'
  ctx.lineWidth = 1
  for (let i = 1; i < 4; i++) {
    const y = (h / 4) * i
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
  }
  for (let i = 1; i < 6; i++) {
    const x = (w / 6) * i
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke()
  }

  const series = [
    { data: props.cpuHistory, color: '#2fb59f', fill: 'rgba(47,181,159,.12)' },
    { data: props.memHistory, color: '#e8a13c', fill: 'rgba(232,161,60,.10)' }
  ]
  const N = 60
  for (const s of series) {
    const pts = s.data.slice(-N)
    if (pts.length < 2) continue
    const step = w / (N - 1)
    const x0 = w - (pts.length - 1) * step
    ctx.beginPath()
    pts.forEach((v, i) => {
      const x = x0 + i * step
      const y = h - (Math.min(100, v) / 100) * (h - 6) - 3
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
    })
    ctx.strokeStyle = s.color
    ctx.lineWidth = 1.8
    ctx.lineJoin = 'round'
    ctx.stroke()
    // 填充
    ctx.lineTo(x0 + (pts.length - 1) * step, h)
    ctx.lineTo(x0, h)
    ctx.closePath()
    ctx.fillStyle = s.fill
    ctx.fill()
  }
}

onMounted(draw)
watch(() => [props.cpuHistory.length, props.memHistory.length, props.memUsageMb], draw)
</script>

<template>
  <div class="chart-wrap">
    <canvas ref="canvas" class="chart" />
    <div class="legend">
      <span class="li"><i class="sw" style="background:#2fb59f" />CPU {{ cpuHistory.at(-1)?.toFixed(1) ?? 0 }}%</span>
      <span class="li"><i class="sw" style="background:#e8a13c" />内存 {{ memUsageMb }}MB</span>
    </div>
  </div>
</template>

<style scoped>
.chart-wrap { position: relative; height: 100%; }
.chart { width: 100%; height: 100%; display: block; }
.legend {
  position: absolute; top: 8px; right: 10px; display: flex; gap: 12px;
  font-size: 11px; color: var(--text-2);
}
.li { display: inline-flex; align-items: center; gap: 5px; }
.sw { width: 8px; height: 8px; border-radius: 2px; }
</style>
