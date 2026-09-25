<script setup>
import { computed } from 'vue'

// 统一分页条：总条数 + 每页条数（分段选择）+ 页码（省略号折叠）
const props = defineProps({
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 }
})
const emit = defineEmits(['update:page', 'update:pageSize', 'change'])

const totalPages = computed(() => Math.max(1, Math.ceil((props.total || 0) / (props.pageSize || 20))))

const pages = computed(() => {
  const t = totalPages.value, c = props.page, out = []
  if (t <= 7) {
    for (let i = 1; i <= t; i++) out.push(i)
    return out
  }
  out.push(1)
  if (c > 3) out.push('…')
  for (let i = Math.max(2, c - 1); i <= Math.min(t - 1, c + 1); i++) out.push(i)
  if (c < t - 2) out.push('…')
  out.push(t)
  return out
})

const SIZES = [20, 50, 100]

function go(p) {
  if (p === '…' || p < 1 || p > totalPages.value || p === props.page) return
  emit('update:page', p)
  emit('change')
}

function setSize(n) {
  if (n === props.pageSize) return
  emit('update:pageSize', n)
  emit('update:page', 1)
  emit('change')
}
</script>

<template>
  <div class="ui-pager">
    <span class="pg-total">共 {{ total }} 条</span>
    <div class="pg-size">
      <button v-for="n in SIZES" :key="n" type="button" class="pg-size-btn"
              :class="{ on: n === pageSize }" @click="setSize(n)">{{ n }}</button>
    </div>
    <div class="pg-btns">
      <button type="button" class="pg-btn" :disabled="page <= 1" @click="go(page - 1)">‹</button>
      <button v-for="(p, i) in pages" :key="i" type="button" class="pg-btn"
              :class="{ on: p === page, dots: p === '…' }" :disabled="p === '…'" @click="go(p)">
        {{ p }}
      </button>
      <button type="button" class="pg-btn" :disabled="page >= totalPages" @click="go(page + 1)">›</button>
    </div>
  </div>
</template>

<style scoped>
.ui-pager {
  display: flex; align-items: center; flex-wrap: wrap; gap: 10px;
  font-size: 12.5px; color: var(--text-2); user-select: none;
}
.pg-total { color: var(--text-dim); white-space: nowrap; }
.pg-size { display: flex; border: 1px solid var(--line); border-radius: 999px; overflow: hidden; }
.pg-size-btn {
  border: none; background: transparent; cursor: pointer; padding: 4px 10px;
  font-size: 12px; font-family: inherit; color: var(--text-dim); transition: all .15s;
}
.pg-size-btn + .pg-size-btn { border-left: 1px solid var(--line); }
.pg-size-btn:hover { color: var(--text); }
.pg-size-btn.on { background: var(--primary-soft); color: var(--primary-deep); font-weight: 600; }
.pg-btns { display: flex; gap: 4px; }
.pg-btn {
  min-width: 26px; height: 26px; padding: 0 6px;
  border: 1px solid var(--line); border-radius: 7px; background: var(--panel);
  font-size: 12.5px; font-family: inherit; color: var(--text-2); cursor: pointer;
  transition: all .15s;
}
.pg-btn:hover:not(:disabled):not(.on) { border-color: var(--primary); color: var(--primary-deep); }
.pg-btn.on {
  background: var(--primary-soft); border-color: var(--primary);
  color: var(--primary-deep); font-weight: 600;
}
.pg-btn.dots { border: none; background: transparent; cursor: default; }
.pg-btn:disabled:not(.dots):not(.on) { opacity: .4; cursor: not-allowed; }
</style>
