<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'

const props = defineProps({
  modelValue: [String, Number],
  options: { type: Array, default: () => [] }, // [{value, label}] 或字符串数组
  disabled: Boolean
})
const emit = defineEmits(['update:modelValue'])

const norm = o => (typeof o === 'object' ? o : { value: o, label: o })
const opts = computed(() => props.options.map(norm))
const cur = computed(() => opts.value.find(o => String(o.value) === String(props.modelValue)))

const open = ref(false)
const root = ref(null)
const listEl = ref(null)
const style = ref({})

async function toggle() {
  if (props.disabled) return
  open.value = !open.value
  if (!open.value) return
  await nextTick()
  const r = root.value.getBoundingClientRect()
  const h = Math.min(listEl.value.scrollHeight, 240)
  const below = window.innerHeight - r.bottom
  const up = below < h + 8 && r.top > h + 8
  style.value = {
    left: Math.min(r.left, window.innerWidth - Math.max(r.width, 160) - 8) + 'px',
    top: (up ? r.top - h - 6 : r.bottom + 4) + 'px',
    width: r.width + 'px'
  }
}

function pick(o) {
  open.value = false
  emit('update:modelValue', o.value)
}

function onDoc(e) {
  const t = e.target
  // 点击触发器或 Teleport 到 body 的下拉列表本身都不关闭（否则 mousedown
  // 先移除列表，click 落空，选择永远无效）
  if (root.value && root.value.contains(t)) return
  if (listEl.value && listEl.value.contains(t)) return
  open.value = false
}
function onScroll() { if (open.value) open.value = false }

onMounted(() => {
  document.addEventListener('mousedown', onDoc)
  window.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', onScroll)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', onDoc)
  window.removeEventListener('scroll', onScroll, true)
  window.removeEventListener('resize', onScroll)
})
</script>

<template>
  <div ref="root" class="ui-select-wrap">
    <button type="button" class="ui-select-trigger" :class="{ open, disabled }" :disabled="disabled" @click="toggle">
      <span class="ui-select-text" :class="{ dim: !cur }">{{ cur ? cur.label : '请选择' }}</span>
      <span class="arrow" aria-hidden="true" />
    </button>
    <Teleport to="body">
      <div v-if="open" ref="listEl" class="ui-select-list" :style="style">
        <button v-for="o in opts" :key="o.value" type="button" class="ui-select-opt"
                :class="{ sel: String(o.value) === String(modelValue) }" @click="pick(o)">
          {{ o.label }}
        </button>
        <div v-if="!opts.length" class="ui-select-empty">暂无选项</div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ui-select-wrap { position: relative; width: 100%; }
.ui-select-trigger {
  width: 100%; height: 36px; padding: 0 30px 0 12px;
  display: flex; align-items: center; justify-content: space-between;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--panel); color: var(--text); font-size: 13px; font-family: inherit;
  cursor: pointer; text-align: left;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.ui-select-trigger:hover:not(:disabled), .ui-select-trigger.open { border-color: var(--primary); }
.ui-select-trigger.open { box-shadow: 0 0 0 3px rgba(47,181,159,.15); }
.ui-select-trigger:disabled { background: var(--bg); cursor: not-allowed; }
.ui-select-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ui-select-text.dim { color: var(--text-dim); }
.arrow {
  position: absolute; right: 12px; top: 50%; pointer-events: none;
  width: 7px; height: 7px; border-right: 1.5px solid var(--text-dim);
  border-bottom: 1.5px solid var(--text-dim);
  transform: translateY(-70%) rotate(45deg);
  transition: transform .15s ease;
}
.arrow { transform-origin: center; }
.ui-select-trigger.open .arrow { transform: translateY(-30%) rotate(-135deg); }
</style>

<style>
/* 下拉列表挂 body（Teleport），非 scoped */
.ui-select-list {
  position: fixed; z-index: 1100;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  box-shadow: 0 12px 32px rgba(15, 40, 34, .16), 0 2px 8px rgba(15, 40, 34, .08);
  padding: 4px; max-height: 240px; overflow-y: auto;
  animation: uiSelIn .14s ease;
}
@keyframes uiSelIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
.ui-select-opt {
  display: block; width: 100%; border: none; background: transparent; cursor: pointer;
  padding: 8px 10px; border-radius: 6px; text-align: left;
  font-size: 13px; font-family: inherit; color: var(--text);
}
.ui-select-opt:hover { background: var(--primary-soft); color: var(--primary-deep); }
.ui-select-opt.sel { background: var(--primary-soft); color: var(--primary-deep); font-weight: 600; }
.ui-select-empty { padding: 10px; font-size: 12.5px; color: var(--text-dim); text-align: center; }
</style>
