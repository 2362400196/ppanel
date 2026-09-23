<script setup>
import { onUnmounted, ref } from 'vue'
import { api, errText, wsUrl } from '../../api/client'
import { toastErr } from '../../components/ui/toast'
import UIButton from '../../components/ui/UIButton.vue'
import UIInput from '../../components/ui/UIInput.vue'
import Terminal from '../../components/Terminal.vue'

const props = defineProps({ instance: { type: Object, required: true } })

const file = ref('requirements.txt')
const running = ref(false)   // 安装任务进行中
const terminal = ref(null)
let ws = null

const canInstall = () => props.instance.status === 'running' && !running.value

async function install() {
  if (!canInstall()) return
  terminal.value?.clear()
  try {
    const { data } = await api.post(`/instances/${props.instance.id}/deps/install`, { file: file.value || 'requirements.txt' })
    running.value = true
    ws = new WebSocket(wsUrl(`/ws/instances/${props.instance.id}/exec`, { job_id: data.job_id }))
    ws.onmessage = e => terminal.value?.write(e.data)
    ws.onclose = () => { running.value = false; ws = null }
    ws.onerror = () => { running.value = false }
  } catch (e) {
    toastErr(errText(e))
  }
}

function stop() {
  ws?.close()
}

onUnmounted(() => ws?.close())
</script>

<template>
  <div class="deps">
    <div class="bar">
      <div class="bar-left">
        <UIInput v-model="file" placeholder="requirements.txt" class="file-input" />
        <UIButton :disabled="!canInstall()" :loading="running" @click="install">安装依赖</UIButton>
        <UIButton v-if="running" type="ghost" @click="stop">断开</UIButton>
      </div>
      <span class="hint" :class="{ warn: instance.status !== 'running' }">
        {{ instance.status === 'running' ? '执行 pip install -r <文件>，输出实时回显' : '容器未运行，请先在概览页启动实例后再安装依赖' }}
      </span>
    </div>
    <div class="term-box">
      <Terminal ref="terminal" placeholder="点击「安装依赖」后，安装输出会在这里实时显示" />
    </div>
  </div>
</template>

<style scoped>
.deps { display: flex; flex-direction: column; gap: 12px; height: calc(100vh - 250px); min-height: 380px; }
.bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.bar-left { display: flex; gap: 8px; align-items: center; }
.file-input { width: 200px; }
.hint { font-size: 12px; color: var(--text-dim); }
.hint.warn { color: var(--warn); }
.term-box { flex: 1; min-height: 0; }
</style>
