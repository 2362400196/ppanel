<script setup>
import { nextTick, ref, watch } from 'vue'
import { taskState, closeTask } from '../api/tasks'

const bodyEl = ref(null)
watch(() => taskState.lines.length, async () => {
  await nextTick()
  if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight
})
</script>

<template>
  <Teleport to="body">
    <transition name="tt-in">
      <div v-if="taskState.open" class="tt-panel">
        <div class="tt-head">
          <span class="tt-dot" :class="taskState.status" />
          <span class="tt-title">
            {{ taskState.status === 'running' ? '任务执行中' : taskState.status === 'done' ? '任务完成' : '任务失败' }}
          </span>
          <span class="tt-id mono">{{ taskState.taskId.slice(0, 8) }}</span>
          <button class="tt-close" title="收起" @click="closeTask">×</button>
        </div>
        <div ref="bodyEl" class="tt-body">
          <div v-for="(l, i) in taskState.lines" :key="i" class="tt-line"
               :class="{ err: l.includes('✘'), ok: l.includes('✔') }">{{ l }}</div>
          <div v-if="taskState.status === 'running'" class="tt-line tt-cursor">▌</div>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<style scoped>
.tt-panel {
  position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%); z-index: 2200;
  width: 640px; max-width: calc(100vw - 48px);
  background: rgba(12, 16, 15, .92); backdrop-filter: blur(14px);
  border: 1px solid rgba(47, 181, 159, .35); border-radius: 12px;
  box-shadow: 0 18px 48px rgba(0, 0, 0, .45);
  overflow: hidden;
}
.tt-head {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 12px; border-bottom: 1px solid rgba(47, 181, 159, .2);
}
.tt-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--primary); animation: ttPulse 1.2s infinite; }
.tt-dot.done { background: #34c07c; animation: none; }
.tt-dot.error { background: #e05260; animation: none; }
@keyframes ttPulse { 0%, 100% { opacity: 1 } 50% { opacity: .35 } }
.tt-title { color: #d8e6e1; font-size: 12.5px; font-weight: 600; }
.tt-id { margin-left: auto; color: rgba(216, 230, 225, .4); font-size: 11px; }
.tt-close {
  border: none; background: transparent; color: rgba(216, 230, 225, .6);
  font-size: 16px; cursor: pointer; padding: 0 2px; line-height: 1;
}
.tt-close:hover { color: #fff; }
.tt-body {
  height: 340px; overflow-y: auto; padding: 10px 12px;
  font-family: var(--font-mono, monospace); font-size: 12px; line-height: 1.7;
}
.tt-line { color: #9fb8ae; white-space: pre-wrap; word-break: break-all; }
.tt-line.err { color: #ff8a94; }
.tt-line.ok { color: #4fd8a8; }
.tt-cursor { color: var(--primary); animation: ttBlink 1s steps(1) infinite; }
@keyframes ttBlink { 50% { opacity: 0 } }
.tt-body::-webkit-scrollbar { width: 6px; }
.tt-body::-webkit-scrollbar-thumb { background: rgba(47, 181, 159, .3); border-radius: 3px; }
.tt-in-enter-active, .tt-in-leave-active { transition: all .2s ease; }
.tt-in-enter-from, .tt-in-leave-to { opacity: 0; transform: translate(-50%, -50%) scale(.96); }
</style>
