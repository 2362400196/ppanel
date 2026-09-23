<script setup>
import { nextTick, ref, watch } from 'vue'

// 终端风格输出区：处理 \r 覆写（pip 进度条）、自动滚动
const props = defineProps({
  placeholder: { type: String, default: '' }
})

const el = ref(null)
const lines = ref([])
const cursor = ref(0) // 当前最后一行的写入位置

function write(chunk) {
  for (const ch of String(chunk)) {
    if (ch === '\r') { cursor.value = 0; continue }
    if (ch === '\n') {
      lines.value.push('')
      cursor.value = 0
      trim()
      continue
    }
    if (!lines.value.length) lines.value.push('')
    const last = lines.value.length - 1
    const line = lines.value[last]
    lines.value[last] = cursor.value < line.length
      ? line.slice(0, cursor.value) + ch + line.slice(cursor.value + 1)
      : line + ch
    cursor.value++
  }
  trim()
}

function trim() {
  if (lines.value.length > 3000) {
    lines.value.splice(0, lines.value.length - 3000)
  }
}

function clear() {
  lines.value = []
  cursor.value = 0
}

watch(lines, () => nextTick(() => {
  if (el.value) el.value.scrollTop = el.value.scrollHeight
}), { deep: true })

defineExpose({ write, clear })
</script>

<template>
  <div ref="el" class="terminal mono">
    <div v-if="!lines.length && placeholder" class="ph">{{ placeholder }}</div>
    <div v-for="(l, i) in lines" v-else :key="i" class="line">{{ l || ' ' }}</div>
  </div>
</template>

<style scoped>
.terminal {
  height: 100%; overflow-y: auto; padding: 14px 16px;
  background: #0c1613; border-radius: var(--radius);
  font-size: 12.5px; line-height: 1.65; color: #a8d5c8;
  white-space: pre-wrap; word-break: break-all;
}
.line { min-height: 1em; }
.ph { color: #44615a; }
</style>
