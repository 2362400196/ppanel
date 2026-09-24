<script setup>
import { onUnmounted, ref } from 'vue'
import { api, errText, wsUrl } from '../../api/client'
import { toastErr } from '../../components/ui/toast'
import UIButton from '../../components/ui/UIButton.vue'
import UISelect from '../../components/ui/UISelect.vue'
import Terminal from '../../components/Terminal.vue'

const props = defineProps({ instance: { type: Object, required: true } })

const tail = ref('200')
const tailOptions = ['100', '200', '500', '1000']
const following = ref(false)
const terminal = ref(null)
let ws = null

async function fetchLogs() {
  try {
    const { data } = await api.get(`/instances/${props.instance.id}/logs`, { params: { tail: tail.value } })
    terminal.value?.clear()
    terminal.value?.write(data.logs || '(暂无日志)')
  } catch (e) {
    toastErr(errText(e))
  }
}

function toggleFollow() {
  if (following.value) {
    ws?.close()
    following.value = false
    return
  }
  terminal.value?.clear()
  ws = new WebSocket(wsUrl(`/ws/instances/${props.instance.id}/logs`, { tail: tail.value }))
  ws.onmessage = e => terminal.value?.write(e.data)
  ws.onclose = () => { following.value = false; ws = null }
  ws.onerror = () => { following.value = false }
  following.value = true
}

onUnmounted(() => ws?.close())
</script>

<template>
  <div class="logs">
    <div class="bar">
      <div class="bar-left">
        <span class="lbl">行数</span>
        <UISelect v-model="tail" :options="tailOptions" class="tail-select" />
        <UIButton type="ghost" @click="fetchLogs">拉取日志</UIButton>
        <UIButton :type="following ? 'danger' : 'primary'" @click="toggleFollow">
          {{ following ? '停止跟踪' : '实时跟踪' }}
        </UIButton>
      </div>
      <span class="hint">{{ following ? '跟踪中，新日志将持续输出' : '拉取最近 N 行，或开启跟踪模式持续输出' }}</span>
    </div>
    <div class="term-box">
      <Terminal placeholder="容器 stdout / stderr 日志" />
    </div>
  </div>
</template>

<style scoped>
.logs { display: flex; flex-direction: column; gap: 12px; height: calc(100vh - 250px); min-height: 380px; }
.bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.bar-left { display: flex; align-items: center; gap: 8px; }
.lbl { font-size: 12px; color: var(--text-dim); }
.tail-select { width: 90px; }
.hint { font-size: 12px; color: var(--text-dim); }
.term-box { flex: 1; min-height: 0; }
</style>
