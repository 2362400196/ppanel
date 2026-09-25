<script setup>
import { computed, ref, watch } from 'vue'
import UIPager from './UIPager.vue'

const props = defineProps({
  columns: { type: Array, required: true }, // [{key, label, width?}]
  rows: { type: Array, default: () => [] },
  loading: Boolean,
  // 服务端分页：传 total（>=0）+ v-model:page + v-model:pageSize，rows 即当前页数据，
  //   翻页/改每页条数时组件 emit('change')，由父级重新拉数据
  // 前端分页：不传 total，rows 全量传入，组件内部自动切片
  total: { type: Number, default: -1 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 }
})
const emit = defineEmits(['update:page', 'update:pageSize', 'change'])

const innerPage = ref(1)
const innerSize = ref(20)
const serverMode = computed(() => props.total >= 0)

const displayRows = computed(() => {
  if (serverMode.value) return props.rows
  const s = (innerPage.value - 1) * innerSize.value
  return props.rows.slice(s, s + innerSize.value)
})
const realTotal = computed(() => (serverMode.value ? props.total : props.rows.length))

// 前端模式：数据变少（删除/搜索）后页码越界时夹住
watch(() => props.rows.length, n => {
  if (serverMode.value) return
  const tp = Math.max(1, Math.ceil(n / innerSize.value))
  if (innerPage.value > tp) innerPage.value = tp
})
// 服务端模式：删除后当前页空了（如最后一页仅剩一条被删）自动回退页码并通知父级重拉
watch(() => props.total, t => {
  if (!serverMode.value || t <= 0 || props.page <= 1) return
  const tp = Math.max(1, Math.ceil(t / props.pageSize))
  if (props.page > tp) {
    emit('update:page', tp)
    emit('change')
  }
})

function fwdPage(v) {
  if (serverMode.value) emit('update:page', v)
  else innerPage.value = v
}
function fwdSize(v) {
  if (serverMode.value) emit('update:pageSize', v)
  else innerSize.value = v
}
function fwdChange() {
  if (serverMode.value) emit('change')
}
</script>

<template>
  <div class="ui-table-wrap">
    <div class="ui-table-scroll">
      <table class="ui-table">
        <thead>
          <tr>
            <th v-for="c in columns" :key="c.key" :style="c.width ? { width: c.width } : null">
              {{ c.label }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td :colspan="columns.length" class="empty">加载中…</td>
          </tr>
          <tr v-else-if="!displayRows.length">
            <td :colspan="columns.length" class="empty">暂无数据</td>
          </tr>
          <tr v-for="(row, i) in displayRows" v-else :key="i" class="row">
            <td v-for="c in columns" :key="c.key">
              <slot :name="'col-' + c.key" :row="row">{{ row[c.key] }}</slot>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="realTotal > 0" class="ui-table-pager">
      <UIPager :total="realTotal"
               :page="serverMode ? page : innerPage"
               :page-size="serverMode ? pageSize : innerSize"
               @update:page="fwdPage"
               @update:page-size="fwdSize"
               @change="fwdChange" />
    </div>
  </div>
</template>

<style scoped>
.ui-table-wrap {
  border: 1px solid var(--line); border-radius: var(--radius);
  overflow: hidden; background: var(--panel);
}
.ui-table-scroll { overflow: auto; }
.ui-table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; padding: 10px 14px; font-weight: 600; color: var(--text-2);
  background: var(--primary-soft-2); border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
td { padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
.row { transition: background .12s ease; }
.row:hover { background: var(--primary-soft-2); }
.empty { text-align: center; color: var(--text-dim); padding: 32px 0; }
.ui-table-pager {
  display: flex; justify-content: flex-end;
  padding: 10px 14px; border-top: 1px solid var(--line); background: var(--panel);
}
</style>
