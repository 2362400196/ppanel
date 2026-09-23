<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../../api/client'
import HistoryChart from '../../components/HistoryChart.vue'

const props = defineProps({ instance: { type: Object, required: true } })

const hours = ref(6)
const points = ref([])

async function load() {
  try {
    const { data } = await api.get(`/instances/${props.instance.id}/metrics`, { params: { hours: hours.value } })
    points.value = data.points || []
  } catch { /* 静默重试 */ }
}

let timer
onMounted(() => {
  load()
  timer = setInterval(load, 60000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="usage">
    <div class="card">
      <div class="card-head">
        <span class="card-title">历史负载 · 最近 {{ hours }} 小时</span>
        <div class="range-switch">
          <button v-for="h in [1, 6, 24]" :key="h" class="range-btn" :class="{ on: hours === h }" @click="hours = h; load()">
            {{ h }}h
          </button>
        </div>
      </div>
      <HistoryChart
        :points="points"
        :series="[
          { key: 'cpu_percent', label: 'CPU', color: '#2fb59f', unit: '%', percent: true },
          { key: 'mem_used_mb', label: '内存', color: '#e8a13c', unit: 'MB' }
        ]"
        :height="200"
      />
    </div>

    <div class="card">
      <div class="card-head"><span class="card-title">网络吞吐 · 最近 {{ hours }} 小时</span></div>
      <HistoryChart
        :points="points"
        :series="[
          { key: 'net_rx_kb_s', label: '下行', color: '#4a90d9', unit: 'KB/s' },
          { key: 'net_tx_kb_s', label: '上行', color: '#b06fd4', unit: 'KB/s' }
        ]"
        :height="200"
      />
    </div>

    <p class="hint text-dim">每 60 秒自动采样一次，数据保留 24 小时。</p>
  </div>
</template>

<style scoped>
.usage { display: flex; flex-direction: column; gap: 16px; }
.card { padding: 16px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.card-title { font-size: 14px; font-weight: 700; }
.range-switch { display: flex; gap: 2px; background: var(--primary-soft-2); border-radius: 8px; padding: 2px; }
.range-btn {
  border: none; background: transparent; font-family: inherit; cursor: pointer;
  font-size: 11px; padding: 3px 10px; border-radius: 6px; color: var(--text-dim);
  transition: all .15s ease;
}
.range-btn:hover { color: var(--text); }
.range-btn.on { background: var(--card-bg, #fff); color: var(--primary-strong); font-weight: 700; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.hint { font-size: 12px; }
</style>
