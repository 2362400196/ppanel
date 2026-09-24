<script setup>
import { watch } from 'vue'

const props = defineProps({
  open: Boolean,
  title: String,
  width: { type: [String, Number], default: '440px' },
  persistent: Boolean // 点击遮罩/Esc 不关闭
})
const emit = defineEmits(['update:open', 'close'])

function close() {
  if (props.persistent) return
  emit('update:open', false)
  emit('close')
}

// 右上角叉号始终可以关闭（persistent 只屏蔽遮罩点击与 Esc）
function closeForced() {
  emit('update:open', false)
  emit('close')
}

function onKey(e) {
  if (e.key === 'Escape') close()
}

watch(() => props.open, v => {
  if (v) document.addEventListener('keydown', onKey)
  else document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="modal-mask" @mousedown.self="close">
        <div class="modal-panel" :style="{ width: typeof width === 'number' ? width + 'px' : width }">
          <div class="modal-head">
            <span class="modal-title">{{ title }}</span>
            <button class="modal-x" aria-label="关闭" @click="closeForced">
              <i />
            </button>
          </div>
          <div class="modal-body"><slot /></div>
          <div v-if="$slots.footer" class="modal-foot"><slot name="footer" /></div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(18, 36, 32, .35);
  backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
}
.modal-panel {
  background: rgba(255, 255, 255, .92);
  backdrop-filter: blur(18px) saturate(1.4);
  border: 1px solid rgba(255, 255, 255, .6);
  border-radius: 16px; box-shadow: var(--shadow-lg);
  max-width: calc(100vw - 32px); max-height: calc(100vh - 64px);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px 0;
}
.modal-title { font-size: 15px; font-weight: 700; }
.modal-x {
  width: 26px; height: 26px; border: none; background: transparent;
  border-radius: 6px; cursor: pointer; position: relative;
}
.modal-x:hover { background: var(--primary-soft); }
.modal-x i, .modal-x i::after {
  content: ''; position: absolute; left: 50%; top: 50%;
  width: 12px; height: 1.6px; background: var(--text-2); border-radius: 1px;
  transform: translate(-50%, -50%) rotate(45deg);
}
.modal-x i::after { transform: translate(-50%, -50%) rotate(-45deg); }
.modal-body { padding: 16px 20px; overflow-y: auto; }
.modal-foot {
  display: flex; justify-content: flex-end; gap: 10px;
  padding: 0 20px 16px;
}

.modal-enter-active, .modal-leave-active { transition: opacity .18s ease; }
.modal-enter-active .modal-panel, .modal-leave-active .modal-panel {
  transition: transform .18s ease, opacity .18s ease;
}
.modal-enter-from, .modal-leave-to { opacity: 0; }
.modal-enter-from .modal-panel, .modal-leave-to .modal-panel {
  transform: translateY(10px) scale(.97); opacity: 0;
}
</style>
