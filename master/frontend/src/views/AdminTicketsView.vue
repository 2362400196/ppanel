<script setup>
// 管理员工单：全部用户工单处理
import { onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr } from '../components/ui/toast'
import UITag from '../components/ui/UITag.vue'
import UITable from '../components/ui/UITable.vue'
import TicketDialog from '../components/TicketDialog.vue'
import { fmtTime } from '../utils/format'

const cols = [
  { key: 'id', label: '编号', width: '70px' },
  { key: 'title', label: '标题' },
  { key: 'username', label: '用户', width: '100px' },
  { key: 'status', label: '状态', width: '96px' },
  { key: 'last_msg', label: '最新动态' },
  { key: 'updated_at', label: '更新时间', width: '170px' },
]

const tickets = ref([])
const loading = ref(true)

async function load() {
  try {
    const { data } = await api.get('/tickets', { params: { all: 1 } })
    tickets.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

const dialogOpen = ref(false)
const dialogEl = ref(null)
const current = ref(0)

function openTicket(row) {
  current.value = row.id
  dialogOpen.value = true
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">工单处理</h1>
        <p class="page-sub">全部用户提交的工单，点击进入会话回复</p>
      </div>
      <UITag tone="warn">{{ tickets.filter(t => t.status === 'open').length }} 个待处理</UITag>
    </div>

    <UITable :columns="cols" :rows="tickets" :loading="loading">
      <template #col-title="{ row }">
        <button class="t-link" @click="openTicket(row)">{{ row.title }}</button>
      </template>
      <template #col-username="{ row }">{{ row.username }}</template>
      <template #col-status="{ row }">
        <UITag :tone="row.status === 'open' ? 'warn' : 'info'">{{ row.status === 'open' ? '待处理' : '已关闭' }}</UITag>
      </template>
      <template #col-last_msg="{ row }">
        <span class="last">{{ row.last_msg || '—' }}</span>
      </template>
      <template #col-updated_at="{ row }">{{ fmtTime(row.updated_at) }}</template>
    </UITable>

    <TicketDialog ref="dialogEl" v-model:open="dialogOpen" :ticket-id="current" admin-mode @changed="load" />
  </div>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.t-link { border: none; background: transparent; cursor: pointer; color: var(--primary-strong); font-size: 13px; font-family: inherit; font-weight: 600; padding: 0; }
.t-link:hover { text-decoration: underline; }
.last { color: var(--text-dim); font-size: 12.5px; }
</style>
