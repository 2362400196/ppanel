<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { fmtTime } from '../utils/format'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITable from '../components/ui/UITable.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import NodeAdminModal from '../components/NodeAdminModal.vue'

const users = ref([])
const instances = ref([])
const instCols = [
  { key: 'id', label: 'ID', width: '56px' },
  { key: 'name', label: '名称' },
  { key: 'owner', label: '归属用户', width: '110px' },
  { key: 'image', label: '镜像' },
  { key: 'ext_port', label: '外部端口', width: '90px' },
  { key: 'status', label: '状态', width: '110px' },
  { key: 'created_at', label: '创建时间', width: '170px' },
  { key: 'ops', label: '', width: '170px' }
]
const adminOpen = ref(false)
const adminNode = ref(null)
const nodeMap = ref({})

function openNodeAdmin(inst) {
  const n = nodeMap.value[inst.node_id]
  if (!n) { toastErr('实例没有可用节点'); return }
  adminNode.value = n
  adminOpen.value = true
}

async function loadNodes() {
  try {
    const { data } = await api.get('/admin/nodes')
    nodeMap.value = Object.fromEntries(data.map(n => [n.id, n]))
  } catch { /* 节点映射失败不阻塞列表 */ }
}

const ownerMap = computed(() => Object.fromEntries(users.value.map(u => [u.id, u.username])))

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

async function loadUsers() {
  try {
    const { data } = await api.get('/admin/users')
    users.value = data
  } catch { /* 归属列失败不阻塞列表 */ }
}

async function loadInstances() {
  try {
    const { data } = await api.get('/instances', { params: { all: 1 } })
    instances.value = data
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

let timer
onMounted(() => {
  loadUsers()
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
        <p class="page-sub">全部用户的实例：测连通、节点管理与停用</p>
      </div>
    </div>

    <div class="sec-head">
      <span class="text-dim">共 {{ instances.length }} 个实例</span>
      <UIButton type="ghost" @click="loadInstances">刷新</UIButton>
    </div>

    <UITable :columns="instCols" :rows="instances">
      <template #col-name="{ row }">
        <button class="link mono" :disabled="openingId === row.id" @click="openPanel(row)" title="进入独立面板">{{ row.name }}</button>
      </template>
      <template #col-owner="{ row }">{{ ownerMap[row.user_id] || `user#${row.user_id}` }}</template>
      <template #col-status="{ row }">
        <StatusDot :status="row.status" />
      </template>
      <template #col-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
      <template #col-ops="{ row }">
        <UIButton type="text" :disabled="probingId === row.id" @click="probe(row)">测连通</UIButton>
        <UIButton type="text" @click="openNodeAdmin(row)">管理</UIButton>
        <UIButton v-if="row.status === 'running'" type="text" @click="power(row)">停用</UIButton>
        <UIButton v-else type="text" :disabled="row.status === 'creating'" @click="power(row)">启动</UIButton>
      </template>
    </UITable>

    <!-- 节点管理面板（概览/容器/镜像/文件/终端/设置） -->
    <NodeAdminModal v-model:open="adminOpen" :node="adminNode" />
  </div>
</template>

<style scoped>
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.link { border: none; background: transparent; cursor: pointer; color: var(--primary-strong); font-size: 13px; font-family: inherit; font-weight: 600; }
.link:hover { text-decoration: underline; }
</style>
