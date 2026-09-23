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

const nodes = ref([])
const loaded = ref(false)

const nodeCols = [
  { key: 'online', label: '状态', width: '110px' },
  { key: 'name', label: '节点名称', width: '160px' },
  { key: 'base_url', label: '被控地址' },
  { key: 'health_text', label: 'Docker 概况' },
  { key: 'enabled', label: '启用', width: '80px' },
  { key: 'ops', label: '', width: '230px' }
]

const healthText = n => {
  if (!n.online) return '-'
  const h = n.health || {}
  return `v${h.server_version || '?'} / ${h.cpus ?? '?'}C ${h.mem_total_gb ?? '?'}GB / 运行 ${h.containers_running ?? 0} 容器`
}

async function loadNodes() {
  try {
    const { data } = await api.get('/admin/nodes')
    nodes.value = data
    loaded.value = true
  } catch (e) {
    toastErr(errText(e))
  }
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
  form.value = { name: n.name, base_url: n.base_url, token: n.token, note: n.note || '' }
  editOpen.value = true
}

async function save() {
  saving.value = true
  try {
    const payload = {
      name: form.value.name.trim(),
      base_url: form.value.base_url.trim(),
      token: form.value.token.trim(),
      note: form.value.note
    }
    if (editing.value) {
      await api.patch(`/admin/nodes/${editId.value}`, payload)
      toastOk('节点已更新')
    } else {
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
      <template #col-health_text="{ row }">
        <span class="text-dim">{{ healthText(row) }}</span>
      </template>
      <template #col-enabled="{ row }">
        <UITag :tone="row.enabled ? 'ok' : 'dim'">{{ row.enabled ? '启用' : '停用' }}</UITag>
      </template>
      <template #col-ops="{ row }">
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
          <UIInput v-model="form.token" class="mono" placeholder="与被控 .env 中 NODE_TOKEN 一致" />
        </label>
        <label class="field">
          <span>备注（可选）</span>
          <UIInput v-model="form.note" placeholder="如：测试节点 / 香港节点" />
        </label>
        <p class="form-tip">保存后主控会立即探测被控健康状态；离线节点也能保存，稍后可用「测连通」重试。</p>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="editOpen = false">取消</UIButton>
        <UIButton :loading="saving" :disabled="!form.name.trim() || !form.base_url.trim() || !form.token.trim()" @click="save">
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
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
.form-tip { font-size: 12px; color: var(--text-dim); margin: 0; line-height: 1.6; }
</style>
