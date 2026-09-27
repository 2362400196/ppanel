<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { api, errText } from '../api/client'
import { startTask } from '../api/tasks'
import { fmtTime } from '../utils/format'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UITable from '../components/ui/UITable.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import UIModal from '../components/ui/UIModal.vue'

const instances = ref([])
const instTotal = ref(0)
const instPage = ref(1)
const instPageSize = ref(20)
const instCols = [
  { key: 'id', label: 'ID', width: '56px' },
  { key: 'name', label: '名称' },
  { key: 'owner', label: '归属用户', width: '110px' },
  { key: 'node', label: '节点', width: '130px' },
  { key: 'image', label: '镜像' },
  { key: 'ext_port', label: '外部端口', width: '90px' },
  { key: 'status', label: '状态', width: '110px' },
  { key: 'uptime', label: '稳定运行', width: '120px' },
  { key: 'created_at', label: '创建时间', width: '170px' },
  { key: 'ops', label: '', width: '210px' }
]
const keyword = ref('')

// 搜索下推到后端（实例名 / 节点名 / 归属用户名），300ms 防抖
let kwTimer
watch(keyword, () => {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(() => {
    instPage.value = 1
    loadInstances()
  }, 300)
})
// 管理：弹窗内直接打开该实例的独立面板（一键登录，iframe 嵌入）
const panelOpen = ref(false)
const panelSrc = ref('')
const panelLoading = ref(false)

async function openInstancePanel(inst) {
  if (panelLoading.value) return
  panelLoading.value = true
  try {
    const { data } = await api.post(`/instances/${inst.id}/panel-token`)
    panelSrc.value = `${data.panel_url}?t=${data.panel_token}`
    panelOpen.value = true
  } catch (e) {
    toastErr(errText(e))
  } finally {
    panelLoading.value = false
  }
}

function closePanel() {
  panelOpen.value = false
  panelSrc.value = ''  // 关闭即卸载 iframe，停止面板内所有请求
}

const nodeMap = ref({})

async function loadNodes() {
  try {
    const { data } = await api.get('/admin/nodes')
    nodeMap.value = Object.fromEntries(data.map(n => [n.id, n]))
  } catch { /* 节点映射失败不阻塞列表 */ }
}

// 点名称直接进入独立面板（一键登录）
const openingId = ref('')
async function openPanel(inst) {
  if (openingId.value) return
  openingId.value = inst.id
  try {
    const { data } = await api.post(`/instances/${inst.id}/panel-token`)
    window.open(`${data.panel_url}?t=${data.panel_token}`, '_blank')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    openingId.value = ''
  }
}

async function loadInstances() {
  try {
    const { data } = await api.get('/instances', {
      params: {
        all: 1, page: instPage.value, page_size: instPageSize.value,
        keyword: keyword.value.trim()
      }
    })
    instances.value = data.items || []
    instTotal.value = data.total || 0
  } catch (e) {
    toastErr(errText(e))
  }
}

// 测连通：TCP 探测实例对外端口
const probingId = ref('')
async function probe(inst) {
  if (probingId.value) return
  probingId.value = inst.id
  try {
    const { data } = await api.post(`/instances/${inst.id}/probe`)
    if (data.ok) toastOk(`连通正常（${data.ms}ms）`)
    else toastErr('端口不通：实例未运行或端口未放行')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    probingId.value = ''
  }
}

async function power(inst) {
  const action = inst.status === 'running' ? 'stop' : 'start'
  try {
    await api.post(`/instances/${inst.id}/${action}`)
    toastOk(action === 'start' ? '已启动' : '已停止')
    loadInstances()
  } catch (e) {
    toastErr(errText(e))
  }
}

// 删除实例（管理员）：回收被控容器/数据 + 清理主控记录
const delOpen = ref(false)
const delTarget = ref(null)
const deleting = ref(false)

function askDelete(inst) { delTarget.value = inst; delOpen.value = true }

async function doDelete() {
  const inst = delTarget.value
  if (!inst || deleting.value) return
  deleting.value = true
  try {
    const tid = startTask('')
    const { data } = await api.delete(`/instances/${inst.id}`,
      { headers: { 'X-Task-Id': tid }, timeout: 600000 })
    toastOk(data.detail || '实例已删除')
    delOpen.value = false
    loadInstances()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    deleting.value = false
  }
}

// 稳定运行时长：上次启动（被控容器 StartedAt）到现在；停止/未运行归零显示
const nowTick = ref(Date.now())
setInterval(() => { nowTick.value = Date.now() }, 30000)

function fmtUptime(row) {
  if (row.status !== 'running') return '已停止'
  if (!row.started_at) return '—'
  const ms = nowTick.value - new Date(row.started_at).getTime()
  if (!(ms > 0)) return '—'
  const m = Math.floor(ms / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时 ${m % 60} 分`
  const d = Math.floor(h / 24)
  return `${d} 天 ${h % 24} 小时`
}

let timer
onMounted(() => {
  loadNodes()
  loadInstances()
  timer = setInterval(loadInstances, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">实例管理</h1>
        <p class="page-sub">全部用户的实例：一键进入独立面板、测连通与停用</p>
      </div>
    </div>

    <div class="sec-head">
      <span class="text-dim">共 {{ instTotal }} 个实例</span>
      <div class="sec-right">
        <UIInput v-model="keyword" style="width:260px" placeholder="搜索：实例名称 / 节点名称 / 归属用户" />
        <UIButton type="ghost" @click="loadInstances">刷新</UIButton>
      </div>
    </div>

    <UITable :columns="instCols" :rows="instances"
             :total="instTotal" v-model:page="instPage" v-model:pageSize="instPageSize"
             @change="loadInstances">
      <template #col-name="{ row }">
        <button class="link mono" :disabled="openingId === row.id" @click="openPanel(row)" title="进入独立面板">{{ row.name }}</button>
      </template>
      <template #col-owner="{ row }">{{ row.owner || `user#${row.user_id}` }}</template>
      <template #col-node="{ row }">
        <span class="node-name">{{ nodeMap[row.node_id]?.name || `节点#${row.node_id}` }}</span>
      </template>
      <template #col-status="{ row }">
        <StatusDot :status="row.status" />
      </template>
      <template #col-uptime="{ row }">
        <span :class="{ 'up-time': row.status === 'running' }">{{ fmtUptime(row) }}</span>
      </template>
      <template #col-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
      <template #col-ops="{ row }">
        <UIButton type="text" :disabled="probingId === row.id" @click="probe(row)">测连通</UIButton>
        <UIButton type="text" :disabled="panelLoading" @click="openInstancePanel(row)">管理</UIButton>
        <UIButton v-if="row.status === 'running'" type="text" @click="power(row)">停用</UIButton>
        <UIButton v-else type="text" :disabled="row.status === 'creating'" @click="power(row)">启动</UIButton>
        <UIButton type="text" class="danger" @click="askDelete(row)">删除</UIButton>
      </template>
    </UITable>

    <!-- 实例独立面板弹窗：一键登录，iframe 直接嵌入被控面板 -->
    <UIModal v-model:open="panelOpen" bare width="94vw" @close="closePanel">
      <iframe v-if="panelSrc" :src="panelSrc" class="panel-frame" />
    </UIModal>

    <ConfirmDialog v-model:open="delOpen" title="删除实例"
                   :message="`确定删除实例「${delTarget?.name || ''}」吗？被控的容器、数据目录与数据库将一并回收，此操作不可恢复。`"
                   confirm-text="删除" @confirm="doDelete" />
  </div>
</template>

<style scoped>
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.sec-right { display: flex; align-items: center; gap: 10px; }
.node-name { font-size: 13px; }
.panel-frame {
  display: block; width: 100%; height: calc(100vh - 64px);  /* 撑满弹窗（max-height 同值），不留白边 */
  border: 0; background: var(--bg);
}
.up-time { color: var(--ok, #30a46c); font-variant-numeric: tabular-nums; }
.link { border: none; background: transparent; cursor: pointer; color: var(--primary-strong); font-size: 13px; font-family: inherit; font-weight: 600; }
.link:hover { text-decoration: underline; }
.danger { color: var(--danger, #e5484d); }
</style>
