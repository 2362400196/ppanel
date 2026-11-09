<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
// 支持从「节点管理」下钻跳转：/manage?keyword=实例名或用户名&node_id=节点ID
const route = useRoute()
const router = useRouter()
const keyword = ref(String(route.query.keyword || ''))

// ---- 按服务器节点切换视图 ----
// curNodeId：'all' = 全部节点，'数字' = 某一台节点；写进 URL，刷新/分享/下钻都能落回同一节点
const nodes = ref([])
const nodeCounts = ref({})   // 后端返回：{ node_id: 实例数 }（随搜索条件变化）
const allTotal = ref(0)      // 全部节点合计（当前搜索条件下）
const curNodeId = ref(String(route.query.node_id || 'all'))
const curNode = computed(() => nodes.value.find(n => String(n.id) === curNodeId.value) || null)
const nodeTabs = computed(() => [
  { id: 'all', name: '全部节点', count: allTotal.value, online: null },
  ...nodes.value.map(n => ({
    id: String(n.id), name: n.name,
    count: nodeCounts.value[String(n.id)] || 0,
    online: !!n.online
  }))
])

function pickNode(id) {
  id = String(id || 'all')
  if (id === curNodeId.value) return
  curNodeId.value = id
  instPage.value = 1
  router.replace({ query: { ...route.query, node_id: id } }).catch(() => {})
  loadInstances()
}
// 外部跳转到本页并带 node_id（如节点管理 → 查看该节点实例）
watch(() => route.query.node_id, v => {
  const next = String(v || '')
  if (!next || next === curNodeId.value) return
  curNodeId.value = next
  instPage.value = 1
  loadInstances()
})

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
    nodes.value = data || []
    nodeMap.value = Object.fromEntries(nodes.value.map(n => [n.id, n]))
    // 未指定节点时默认停在第一台节点（不再把所有节点实例混在一个列表里）
    if (!route.query.node_id && nodes.value.length) {
      curNodeId.value = String(nodes.value[0].id)
      router.replace({ query: { ...route.query, node_id: curNodeId.value } }).catch(() => {})
    }
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
    const params = {
      all: 1, page: instPage.value, page_size: instPageSize.value,
      keyword: keyword.value.trim()
    }
    if (/^\d+$/.test(curNodeId.value)) params.node_id = Number(curNodeId.value)
    const { data } = await api.get('/instances', { params })
    instances.value = data.items || []
    instTotal.value = data.total || 0
    nodeCounts.value = data.node_counts || {}
    allTotal.value = data.all_total ?? instTotal.value
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
onMounted(async () => {
  await loadNodes()      // 先确定默认节点，再按该节点拉列表
  await loadInstances()
  timer = setInterval(loadInstances, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">实例管理</h1>
        <p class="page-sub">按服务器节点分别查看实例：一键进入独立面板、测连通与停用</p>
      </div>
    </div>

    <!-- 节点切换：每台服务器一个页签，数量为该节点实例数（随搜索条件变化） -->
    <div v-if="nodes.length" class="node-bar">
      <span class="node-lbl">服务器节点</span>
      <div class="node-tabs">
        <button v-for="t in nodeTabs" :key="t.id" type="button" class="node-tab"
                :class="{ on: curNodeId === t.id }" @click="pickNode(t.id)">
          <span v-if="t.online !== null" class="node-dot" :class="t.online ? 'ok' : 'off'" />
          <span>{{ t.name }}</span>
          <span class="node-cnt">{{ t.count }}</span>
        </button>
      </div>
      <span v-if="curNode" class="node-hint">
        <i class="node-dot" :class="curNode.online ? 'ok' : 'off'" />
        {{ curNode.online ? '在线' : '离线' }} · {{ curNode.base_url }}
      </span>
    </div>

    <div class="sec-head">
      <span class="text-dim">
        共 {{ instTotal }} 个实例<span v-if="curNode"> · {{ curNode.name }}</span>
      </span>
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
    <UIModal v-model:open="panelOpen" bare fullscreen width="100vw" @close="closePanel">
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
.node-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
.node-lbl { font-size: 13px; font-weight: 600; color: var(--text-2); white-space: nowrap; }
.node-tabs {
  display: inline-flex; gap: 4px; padding: 4px; border-radius: 10px;
  background: #eef3f2; max-width: 100%; overflow-x: auto;
}
.node-tab {
  display: inline-flex; align-items: center; gap: 7px; white-space: nowrap;
  height: 32px; padding: 0 12px; border: none; border-radius: 7px;
  background: transparent; color: var(--text-2); font: inherit; font-size: 13px;
  cursor: pointer; transition: all .15s ease;
}
.node-tab:hover { color: var(--primary-strong); }
.node-tab.on {
  background: var(--panel); color: var(--primary-strong); font-weight: 600;
  box-shadow: 0 2px 8px rgba(28,54,48,.1);
}
.node-cnt {
  min-width: 20px; padding: 1px 6px; border-radius: 9px; text-align: center;
  background: rgba(28,54,48,.08); font-size: 11.5px; font-variant-numeric: tabular-nums;
}
.node-tab.on .node-cnt { background: var(--primary-soft); color: var(--primary-deep); }
.node-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; }
.node-dot.ok { background: var(--ok, #30a46c); }
.node-dot.off { background: #b9c6c2; }
.node-hint { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-dim); }
.panel-frame {
  display: block; width: 100%; height: 100vh;  /* 全屏弹窗：铺满视口 */
  border: 0; background: var(--bg);
}
.up-time { color: var(--ok, #30a46c); font-variant-numeric: tabular-nums; }
.link { border: none; background: transparent; cursor: pointer; color: var(--primary-strong); font-size: 13px; font-family: inherit; font-weight: 600; }
.link:hover { text-decoration: underline; }
.danger { color: var(--danger, #e5484d); }
</style>
