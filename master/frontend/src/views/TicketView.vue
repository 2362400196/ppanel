<script setup>
// 用户工单：列表 + 新建 + 会话
import { onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UITag from '../components/ui/UITag.vue'
import UITable from '../components/ui/UITable.vue'
import UIModal from '../components/ui/UIModal.vue'
import TicketDialog from '../components/TicketDialog.vue'
import { fmtTime } from '../utils/format'

const cols = [
  { key: 'id', label: '编号', width: '70px' },
  { key: 'title', label: '标题' },
  { key: 'status', label: '状态', width: '96px' },
  { key: 'last_msg', label: '最新动态' },
  { key: 'updated_at', label: '更新时间', width: '170px' },
]

const tickets = ref([])
const loading = ref(true)

async function load() {
  try {
    const { data } = await api.get('/tickets')
    tickets.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

// 新建工单
const createOpen = ref(false)
const form = ref({ title: '', content: '' })
const creating = ref(false)

async function doCreate() {
  if (!form.value.title.trim() || !form.value.content.trim()) {
    toastErr('请填写标题和问题描述')
    return
  }
  creating.value = true
  try {
    await api.post('/tickets', form.value)
    toastOk('工单已提交，管理员会尽快处理')
    createOpen.value = false
    form.value = { title: '', content: '' }
    load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    creating.value = false
  }
}

// 会话
const dialogOpen = ref(false)
const dialogEl = ref(null)
function openTicket(row) {
  dialogOpen.value = true
  setTimeout(() => dialogEl.value?.load(), 50)  // 弹窗 watch 生效后加载
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">工单</h1>
        <p class="page-sub">遇到问题或需要帮助？提交工单，管理员会尽快回复</p>
      </div>
      <UIButton @click="createOpen = true">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14" /></svg>
        新工单
      </UIButton>
    </div>

    <UITable :columns="cols" :rows="tickets" :loading="loading">
      <template #col-title="{ row }">
        <button class="t-link" @click="openTicket(row)">{{ row.title }}</button>
      </template>
      <template #col-status="{ row }">
        <UITag :tone="row.status === 'open' ? 'ok' : 'info'">{{ row.status === 'open' ? '处理中' : '已关闭' }}</UITag>
      </template>
      <template #col-last_msg="{ row }">
        <span class="last" :class="{ admin: row.last_by_admin }">{{ row.last_msg || '—' }}</span>
      </template>
      <template #col-updated_at="{ row }">{{ fmtTime(row.updated_at) }}</template>
    </UITable>

    <!-- 新建工单 -->
    <UIModal v-model:open="createOpen" title="提交工单" width="440px">
      <div class="form">
        <label class="field">
          <span>标题</span>
          <UIInput v-model="form.title" placeholder="一句话概括问题" />
        </label>
        <label class="field">
          <span>问题描述</span>
          <textarea v-model="form.content" rows="5" placeholder="详细描述遇到的问题，可附截图说明文字…" />
        </label>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="createOpen = false">取消</UIButton>
        <UIButton :loading="creating" @click="doCreate">提交</UIButton>
      </template>
    </UIModal>

    <!-- 会话 -->
    <TicketDialog ref="dialogEl" v-model:open="dialogOpen" @changed="load" />
  </div>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.t-link { border: none; background: transparent; cursor: pointer; color: var(--primary-strong); font-size: 13px; font-family: inherit; font-weight: 600; padding: 0; }
.t-link:hover { text-decoration: underline; }
.last { color: var(--text-dim); font-size: 12.5px; }
.last.admin { color: var(--primary-strong); }
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
.field textarea {
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel); resize: vertical;
  padding: 9px 12px; font-size: 13px; font-family: inherit; color: var(--text); line-height: 1.6;
}
.field textarea:focus { outline: none; border-color: var(--primary); }
</style>
