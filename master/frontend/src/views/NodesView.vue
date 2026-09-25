<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITable from '../components/ui/UITable.vue'
import UITag from '../components/ui/UITag.vue'
import UIModal from '../components/ui/UIModal.vue'
import UIInput from '../components/ui/UIInput.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import NodeAdminModal from '../components/NodeAdminModal.vue'

const nodes = ref([])
const loaded = ref(false)

const nodeCols = [
  { key: 'online', label: '状态', width: '110px' },
  { key: 'name', label: '节点名称', width: '160px' },
  { key: 'base_url', label: '被控地址' },
  { key: 'stability', label: '稳定性', width: '150px' },
  { key: 'health_text', label: 'Docker 概况' },
  { key: 'enabled', label: '启用', width: '80px' },
  { key: 'ops', label: '', width: '330px' }
]

// 节点管理面板
const adminOpen = ref(false)
const adminNode = ref(null)
function openNodeAdmin(n) {
  adminNode.value = n
  adminOpen.value = true
}

const healthText = n => {
  if (!n.online) return '-'
  const h = n.health || {}
  return `v${h.server_version || '?'} / ${h.cpus ?? '?'}C ${h.mem_total_gb ?? '?'}GB / 运行 ${h.containers_running ?? 0} 容器`
}

// 节点实例清单：这台服务器上有哪些用户的哪些实例
const instOpen = ref(false)
const instNode = ref(null)
const instRows = ref([])
const instLoading = ref(false)
const ownerMap = ref({})

const instCols = [
  { key: 'owner', label: '归属用户', width: '120px' },
  { key: 'name', label: '实例名称' },
  { key: 'image', label: '镜像', width: '170px' },
  { key: 'status', label: '状态', width: '110px' },
  { key: 'ext_port', label: '外部端口', width: '90px' },
  { key: 'created_at', label: '创建时间', width: '160px' }
]

async function openNodeInstances(n) {
  instNode.value = n
  instOpen.value = true
  instLoading.value = true
  try {
    const [a, b] = await Promise.all([
      api.get('/instances', { params: { all: 1 } }),
      api.get('/admin/users')
    ])
    ownerMap.value = Object.fromEntries(b.data.map(u => [u.id, u.username]))
    instRows.value = a.data.filter(i => String(i.node_id) === String(n.id))
  } catch (e) {
    toastErr(errText(e))
  } finally {
    instLoading.value = false
  }
}

const instRunning = () => instRows.value.filter(i => i.status === 'running').length

function fmtWhen(v) {
  if (!v) return '-'
  const d = new Date(v)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function loadNodes() {
  try {
    const [a, s] = await Promise.all([
      api.get('/admin/nodes'),
      api.get('/admin/nodes/stability', { params: { window: 7 } }).catch(() => null),
    ])
    nodes.value = a.data
    stab.value = s?.data || {}
    loaded.value = true
  } catch (e) {
    toastErr(errText(e))
  }
}

// 稳定性评分（7 天窗口）：徽标 + 悬停四维拆分
const stab = ref({})
const stabTone = g => ({ excellent: 'ok', good: 'ok', fair: 'warn', poor: 'warn', observing: 'dim' }[g] || 'dim')
const stabText = s => {
  if (!s) return ''
  const p = s.parts || {}
  const seg = []
  if (s.score != null) seg.push(`综合 ${s.score}`)
  if (p.crash != null) seg.push(`崩溃 ${p.crash}`)
  if (p.online != null) seg.push(`在线 ${p.online}`)
  if (p.resource != null) seg.push(`资源 ${p.resource}`)
  if (p.service != null) seg.push(`服务 ${p.service}`)
  if (s.crashes) seg.push(`崩溃 ${s.crashes} 次`)
  if (s.mem_peak != null) seg.push(`内存峰值 ${s.mem_peak}%`)
  if (s.disk_peak != null) seg.push(`磁盘峰值 ${s.disk_peak}%`)
  return `近 ${s.window_days} 天：${seg.join(' / ')}`
}

// 新增 / 编辑
const editOpen = ref(false)
const editing = ref(false)           // false=新增，true=编辑
const editId = ref(null)
const form = ref({ name: '', base_url: '', token: '', note: '' })
const saving = ref(false)

function openCreate() {
  editing.value = false
  editId.value = null
  form.value = { name: '', base_url: 'http://', token: '', note: '' }
  editOpen.value = true
}
function openEdit(n) {
  editing.value = true
  editId.value = n.id
  // 后端出于安全不回传 token，编辑时留空 = 保持原 token 不变
  form.value = { name: n.name || '', base_url: n.base_url || '', token: '', note: n.note || '' }
  editOpen.value = true
}

async function save() {
  saving.value = true
  try {
    const payload = {
      name: form.value.name.trim(),
      base_url: form.value.base_url.trim(),
      note: form.value.note
    }
    if (form.value.token.trim()) payload.token = form.value.token.trim()
    if (editing.value) {
      await api.patch(`/admin/nodes/${editId.value}`, payload)
      toastOk('节点已更新')
    } else {
      if (!payload.token) { toastErr('请填写节点 Token'); saving.value = false; return }
      await api.post('/admin/nodes', payload)
      toastOk('节点已接入')
    }
    editOpen.value = false
    loadNodes()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(n) {
  try {
    await api.patch(`/admin/nodes/${n.id}`, { enabled: !n.enabled })
    toastOk(n.enabled ? '已停用' : '已启用')
    loadNodes()
  } catch (e) {
    toastErr(errText(e))
  }
}

async function testNode(n) {
  try {
    const { data } = await api.post(`/admin/nodes/${n.id}/test`)
    if (data.online) toastOk(`连通正常：Docker ${data.health.server_version}`)
    else toastErr('节点不可达或 token 无效')
    loadNodes()
  } catch (e) {
    toastErr(errText(e))
  }
}

const delOpen = ref(false)
const delTarget = ref(null)
async function doDelete() {
  try {
    const { data } = await api.delete(`/admin/nodes/${delTarget.value.id}`)
    toastOk(data.detail || '已删除')
    delOpen.value = false
    loadNodes()
  } catch (e) {
    toastErr(errText(e))
  }
}

let timer
onMounted(() => {
  loadNodes()
  timer = setInterval(loadNodes, 15000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">节点管理</h1>
        <p class="page-sub">接入被控 Agent，主控通过它编排容器实例</p>
      </div>
      <UIButton @click="openCreate">接入节点</UIButton>
    </div>

    <div v-if="loaded && !nodes.length" class="empty-tip">
      还没有接入任何节点。在被控服务器上部署 Agent 后，填入地址与 token 即可接入。
    </div>

    <UITable v-else :columns="nodeCols" :rows="nodes">
      <template #col-online="{ row }">
        <StatusDot :status="row.online ? 'running' : 'exited'" />
        <span class="ml4">{{ row.online ? '在线' : '离线' }}</span>
      </template>
      <template #col-name="{ row }">
        <span class="name-strong">{{ row.name }}</span>
        <UITag v-if="row.note" tone="dim" class="ml6">{{ row.note }}</UITag>
      </template>
      <template #col-base_url="{ row }"><span class="mono text-dim">{{ row.base_url }}</span></template>
      <template #col-stability="{ row }">
        <UITag v-if="stab[row.id]" :tone="stabTone(stab[row.id].grade)" :title="stabText(stab[row.id])">
          {{ stab[row.id].score != null ? `${stab[row.id].score} 分 · ${stab[row.id].grade_label}` : stab[row.id].grade_label }}
        </UITag>
        <span v-else class="text-dim">—</span>
      </template>
      <template #col-health_text="{ row }">
        <span class="text-dim">{{ healthText(row) }}</span>
      </template>
      <template #col-enabled="{ row }">
        <UITag :tone="row.enabled ? 'ok' : 'dim'">{{ row.enabled ? '启用' : '停用' }}</UITag>
      </template>
      <template #col-ops="{ row }">
        <UIButton type="text" @click="openNodeAdmin(row)">管理</UIButton>
        <UIButton type="text" @click="openNodeInstances(row)">实例</UIButton>
        <UIButton type="text" @click="testNode(row)">测连通</UIButton>
        <UIButton type="text" @click="openEdit(row)">编辑</UIButton>
        <UIButton type="text" @click="toggleEnabled(row)">{{ row.enabled ? '停用' : '启用' }}</UIButton>
        <UIButton type="text" class="danger" @click="delTarget = row; delOpen = true">删除</UIButton>
      </template>
    </UITable>

    <!-- 接入 / 编辑节点 -->
    <UIModal v-model:open="editOpen" :title="editing ? '编辑节点' : '接入节点'" width="440px">
      <div class="form">
        <label class="field">
          <span>节点名称</span>
          <UIInput v-model="form.name" placeholder="如 wsl-ubuntu / hk-01" />
        </label>
        <label class="field">
          <span>被控地址</span>
          <UIInput v-model="form.base_url" class="mono" placeholder="http://1.2.3.4:9100" />
        </label>
        <label class="field">
          <span>节点 Token（X-Node-Token）</span>
          <UIInput v-model="form.token" class="mono" :placeholder="editing ? '留空保持原 Token 不变；填写则重置' : '与被控 .env 中 NODE_TOKEN 一致'" />
        </label>
        <label class="field">
          <span>备注（可选）</span>
          <UIInput v-model="form.note" placeholder="如：测试节点 / 香港节点" />
        </label>
        <p class="form-tip">保存后主控会立即探测被控健康状态；离线节点也能保存，稍后可用「测连通」重试。</p>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="editOpen = false">取消</UIButton>
        <UIButton :loading="saving" :disabled="!form.name.trim() || !form.base_url.trim() || (!editing && !form.token.trim())" @click="save">
          {{ editing ? '保存' : '接入' }}
        </UIButton>
      </template>
    </UIModal>

    <ConfirmDialog
      v-model:open="delOpen"
      title="删除节点"
      :message="`确定删除节点「${delTarget?.name}」吗？仅解除主控关联，不影响被控服务器上已运行的实例。`"
      confirm-text="删除"
      danger
      @confirm="doDelete"
    />

    <!-- 节点实例清单：哪些用户 / 哪些实例 -->
    <UIModal v-model:open="instOpen" :title="`节点实例 - ${instNode?.name || ''}`" width="760px">
      <p class="inst-summary">
        这台服务器上共有 <b>{{ instRows.length }}</b> 个实例，<b>{{ instRunning() }}</b> 个运行中
      </p>
      <UITable :columns="instCols" :rows="instRows" :loading="instLoading">
        <template #col-owner="{ row }">
          <span class="owner">{{ ownerMap[row.user_id] || `user#${row.user_id}` }}</span>
        </template>
        <template #col-status="{ row }"><StatusDot :status="row.status" /></template>
        <template #col-ext_port="{ row }"><span class="mono">{{ row.ext_port || '-' }}</span></template>
        <template #col-created_at="{ row }"><span class="text-dim">{{ fmtWhen(row.created_at) }}</span></template>
      </UITable>
      <div v-if="!instLoading && !instRows.length" class="inst-empty">该节点上还没有任何实例。</div>
    </UIModal>

    <!-- 节点管理面板（概览/容器/镜像/文件管理/宿主机终端） -->
    <NodeAdminModal v-model:open="adminOpen" :node="adminNode" />
  </div>
</template>

<style scoped>
.ml4 { margin-left: 4px; }
.ml6 { margin-left: 6px; }
.name-strong { font-weight: 600; }
.empty-tip {
  background: var(--card-bg); border: 1px dashed var(--line); border-radius: var(--radius);
  padding: 42px; text-align: center; color: var(--text-dim); font-size: 13px; line-height: 1.8;
}
.form { display: flex; flex-direction: column; gap: 14px; }
.inst-summary { margin: 0 0 12px; font-size: 13px; color: var(--text-2); }
.inst-summary b { color: var(--primary-deep); font-family: var(--font-mono); }
.owner { font-weight: 600; }
.inst-empty { text-align: center; color: var(--text-dim); font-size: 13px; padding: 18px 0 6px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
.form-tip { font-size: 12px; color: var(--text-dim); margin: 0; line-height: 1.6; }
</style>
