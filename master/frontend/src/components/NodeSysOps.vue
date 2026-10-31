<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from './ui/toast'
import UIButton from './ui/UIButton.vue'
import UITabs from './ui/UITabs.vue'
import UITable from './ui/UITable.vue'
import UITag from './ui/UITag.vue'
import UIInput from './ui/UIInput.vue'
import UISelect from './ui/UISelect.vue'
import ConfirmDialog from './ui/ConfirmDialog.vue'

const props = defineProps({
  nodeId: { type: [String, Number], default: '' }
})

const tabs = [
  { key: 'processes', label: '进程' },
  { key: 'services', label: '服务' },
  { key: 'ports', label: '端口' },
  { key: 'logs', label: '系统日志' },
  { key: 'logins', label: '登录审计' }
]
const tab = ref('processes')

const np = () => ({ node: String(props.nodeId || '') })

// ---------- 进程 ----------
const procs = ref({ items: [], total: 0 })
const procSort = ref('cpu')
const procLoading = ref(false)

async function loadProcs() {
  if (!props.nodeId) return
  procLoading.value = true
  try {
    const { data } = await api.get('/host/processes', {
      params: { ...np(), sort: procSort.value, limit: 100 }
    })
    procs.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    procLoading.value = false
  }
}

const procCols = [
  { key: 'pid', label: 'PID', width: '90px' },
  { key: 'user', label: '用户', width: '110px' },
  { key: 'cpu', label: 'CPU %', width: '90px' },
  { key: 'mem', label: 'MEM %', width: '90px' },
  { key: 'rss', label: '内存', width: '100px' },
  { key: 'etime', label: '运行时长', width: '110px' },
  { key: 'cmd', label: '命令' }
]

// ---------- 服务 ----------
const svcs = ref({ items: [], counts: {} })
const svcLoading = ref(false)
const svcQuery = ref('')
const svcFilter = ref('all')

async function loadSvcs() {
  if (!props.nodeId) return
  svcLoading.value = true
  try {
    const { data } = await api.get('/host/services', { params: np(), timeout: 30000 })
    svcs.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    svcLoading.value = false
  }
}

const svcRows = computed(() => {
  const q = svcQuery.value.trim().toLowerCase()
  let rows = svcs.value.items || []
  if (svcFilter.value === 'running') rows = rows.filter(r => r.sub === 'running')
  else if (svcFilter.value === 'failed') rows = rows.filter(r => r.active === 'failed' || r.sub === 'failed')
  if (q) rows = rows.filter(r => r.unit.toLowerCase().includes(q) || (r.description || '').toLowerCase().includes(q))
  return rows
})

const svcCols = [
  { key: 'unit', label: '服务单元' },
  { key: 'active', label: '状态', width: '110px' },
  { key: 'enabled', label: '开机自启', width: '110px' },
  { key: 'description', label: '说明' },
  { key: 'ops', label: '', width: '230px' }
]

const act = ref({ open: false, unit: '', action: '', loading: false })
const actText = computed(() => {
  const map = { start: '启动', stop: '停止', restart: '重启', enable: '设为开机自启', disable: '取消开机自启' }
  return map[act.value.action] || act.value.action
})

function askAction(unit, action) {
  if (action === 'stop' || action === 'disable') {
    act.value = { open: true, unit, action, loading: false }
  } else {
    doAction(unit, action)
  }
}

async function doAction(unit, action) {
  act.value.loading = true
  try {
    await api.post('/host/services/action', { unit, action }, { params: np(), timeout: 90000 })
    toastOk(`${unit} ${action} 成功`)
    act.value.open = false
    await loadSvcs()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    act.value.loading = false
  }
}

// ---------- 端口 ----------
const ports = ref({ items: [], total: 0 })
const portLoading = ref(false)
const portQuery = ref('')

async function loadPorts() {
  if (!props.nodeId) return
  portLoading.value = true
  try {
    const { data } = await api.get('/host/ports', { params: np() })
    ports.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    portLoading.value = false
  }
}

const portRows = computed(() => {
  const q = portQuery.value.trim()
  const rows = ports.value.items || []
  if (!q) return rows
  return rows.filter(
    r => String(r.port).includes(q) || (r.process || '').includes(q) || (r.addr || '').includes(q)
  )
})

const portCols = [
  { key: 'port', label: '端口', width: '90px' },
  { key: 'proto', label: '协议', width: '80px' },
  { key: 'addr', label: '监听地址', width: '180px' },
  { key: 'process', label: '进程', width: '200px' },
  { key: 'pid', label: 'PID', width: '100px' }
]

// ---------- 系统日志 ----------
const logs = ref({ lines: [] })
const logUnit = ref('')
const logLines = ref(200)
const logLoading = ref(false)

const logUnitOptions = computed(() => [
  { value: '', label: '全部日志' },
  ...(svcs.value.items || []).map(s => ({ value: s.unit, label: s.unit }))
])

async function loadLogs() {
  if (!props.nodeId) return
  logLoading.value = true
  try {
    const { data } = await api.get('/host/journal', {
      params: { ...np(), unit: logUnit.value, lines: logLines.value },
      timeout: 30000
    })
    logs.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    logLoading.value = false
  }
}

// ---------- 登录审计 ----------
const logins = ref({ items: [], accepted: 0, failed: 0 })
const accounts = ref([])
const loginLoading = ref(false)

async function loadLogins() {
  if (!props.nodeId) return
  loginLoading.value = true
  try {
    const [r1, r2] = await Promise.all([
      api.get('/host/ssh-logins', { params: { ...np(), limit: 200 } }),
      api.get('/host/users', { params: np() })
    ])
    logins.value = r1.data
    accounts.value = r2.data?.accounts || []
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loginLoading.value = false
  }
}

const loginCols = [
  { key: 'ok', label: '结果', width: '90px' },
  { key: 'line', label: '记录' }
]

// ---------- 切换 ----------
function loadTab() {
  if (tab.value === 'processes') loadProcs()
  else if (tab.value === 'services') { if (!svcs.value.items.length) loadSvcs() }
  else if (tab.value === 'ports') loadPorts()
  else if (tab.value === 'logs') { if (!svcs.value.items.length) loadSvcs(); loadLogs() }
  else if (tab.value === 'logins') loadLogins()
}

watch(tab, loadTab)
watch(() => props.nodeId, () => {
  if (!props.nodeId) return
  // 切换节点：清空缓存数据重新拉取
  procs.value = { items: [], total: 0 }
  svcs.value = { items: [], counts: {} }
  ports.value = { items: [], total: 0 }
  logs.value = { lines: [] }
  logins.value = { items: [], accepted: 0, failed: 0 }
  accounts.value = []
  tab.value = 'processes'
  loadTab()
})

onMounted(loadTab)

// ---------- 格式化 ----------
const fmtBytes = n => {
  if (!n) return '0'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i > 1 ? 1 : 0)} ${u[i]}`
}
const svcTone = r => (r.active === 'failed' || r.sub === 'failed' ? 'danger' : r.sub === 'running' ? 'ok' : 'dim')
const svcState = r => (r.active === 'failed' || r.sub === 'failed' ? 'failed' : r.sub)
</script>

<template>
  <div>
    <div v-if="!nodeId" class="no-node-tip">未选择节点</div>
    <template v-else>
      <UITabs v-model="tab" :tabs="tabs" class="tabs" />

      <!-- 进程 -->
      <template v-if="tab === 'processes'">
        <div class="sec-head">
          <span class="text-dim">共 {{ procs.total }} 个进程，按{{ procSort === 'cpu' ? ' CPU' : '内存' }}排序显示前 {{ procs.items.length }} 条</span>
          <div class="row-gap">
            <UISelect v-model="procSort" :options="[{ value: 'cpu', label: '按 CPU' }, { value: 'mem', label: '按内存' }]" class="w150" @update:model-value="loadProcs" />
            <UIButton type="ghost" @click="loadProcs">刷新</UIButton>
          </div>
        </div>
        <UITable :columns="procCols" :rows="procs.items" :loading="procLoading">
          <template #col-pid="{ row }"><span class="mono">{{ row.pid }}</span></template>
          <template #col-user="{ row }"><span class="text-dim">{{ row.user }}</span></template>
          <template #col-cpu="{ row }"><span class="mono" :class="{ hot: row.cpu >= 50 }">{{ row.cpu }}</span></template>
          <template #col-mem="{ row }"><span class="mono">{{ row.mem }}</span></template>
          <template #col-rss="{ row }"><span class="mono">{{ fmtBytes(row.rss) }}</span></template>
          <template #col-etime="{ row }"><span class="mono text-dim">{{ row.etime }}</span></template>
          <template #col-cmd="{ row }"><span class="mono cmd-cell" :title="row.cmd">{{ row.cmd }}</span></template>
        </UITable>
      </template>

      <!-- 服务 -->
      <template v-else-if="tab === 'services'">
        <div class="sec-head">
          <span class="text-dim">
            共 {{ svcs.counts.total || 0 }} 个服务 · 运行中 {{ svcs.counts.running || 0 }} ·
            失败 {{ svcs.counts.failed || 0 }} · 系统状态 {{ svcs.system_state || '-' }}
          </span>
          <div class="row-gap">
            <UIInput v-model="svcQuery" placeholder="搜索服务名或说明" class="w220" />
            <UISelect v-model="svcFilter" :options="[
              { value: 'all', label: '全部' },
              { value: 'running', label: '仅运行中' },
              { value: 'failed', label: '仅失败' }
            ]" class="w120" />
            <UIButton type="ghost" @click="loadSvcs">刷新</UIButton>
          </div>
        </div>
        <UITable :columns="svcCols" :rows="svcRows" :loading="svcLoading">
          <template #col-unit="{ row }"><span class="mono">{{ row.unit }}</span></template>
          <template #col-active="{ row }"><UITag :tone="svcTone(row)">{{ svcState(row) }}</UITag></template>
          <template #col-enabled="{ row }">
            <span class="text-dim">{{ row.enabled }}</span>
          </template>
          <template #col-description="{ row }"><span class="text-dim desc-cell">{{ row.description }}</span></template>
          <template #col-ops="{ row }">
            <UIButton v-if="row.sub !== 'running'" type="text" @click="askAction(row.unit, 'start')">启动</UIButton>
            <template v-else>
              <UIButton type="text" @click="askAction(row.unit, 'restart')">重启</UIButton>
              <UIButton type="text" class="danger" @click="askAction(row.unit, 'stop')">停止</UIButton>
            </template>
            <UIButton v-if="row.enabled !== 'enabled'" type="text" @click="askAction(row.unit, 'enable')">设自启</UIButton>
            <UIButton v-else type="text" class="danger" @click="askAction(row.unit, 'disable')">取消自启</UIButton>
          </template>
        </UITable>
      </template>

      <!-- 端口 -->
      <template v-else-if="tab === 'ports'">
        <div class="sec-head">
          <span class="text-dim">共 {{ ports.total }} 个监听端口</span>
          <div class="row-gap">
            <UIInput v-model="portQuery" placeholder="按端口 / 进程 / 地址过滤" class="w220" />
            <UIButton type="ghost" @click="loadPorts">刷新</UIButton>
          </div>
        </div>
        <UITable :columns="portCols" :rows="portRows" :loading="portLoading">
          <template #col-port="{ row }"><span class="mono strong">{{ row.port }}</span></template>
          <template #col-proto="{ row }"><UITag tone="dim">{{ row.proto }}</UITag></template>
          <template #col-addr="{ row }"><span class="mono text-dim">{{ row.addr }}</span></template>
          <template #col-process="{ row }"><span class="mono">{{ row.process || '-' }}</span></template>
          <template #col-pid="{ row }"><span class="mono text-dim">{{ row.pid || '-' }}</span></template>
        </UITable>
      </template>

      <!-- 系统日志 -->
      <template v-else-if="tab === 'logs'">
        <div class="sec-head">
          <span class="text-dim">journald 最近日志</span>
          <div class="row-gap">
            <UISelect v-model="logUnit" :options="logUnitOptions" class="w260" />
            <UISelect v-model="logLines" :options="[{ value: 100, label: '100 行' }, { value: 200, label: '200 行' }, { value: 500, label: '500 行' }]" class="w110" />
            <UIButton type="ghost" @click="loadLogs">刷新</UIButton>
          </div>
        </div>
        <div v-if="logs.error" class="err-box">{{ logs.error }}</div>
        <div class="log-box" :class="{ loading: logLoading }">
          <div v-if="!logs.lines.length" class="text-dim pad">暂无日志</div>
          <div v-for="(l, i) in logs.lines" :key="i" class="log-line mono">{{ l }}</div>
        </div>
      </template>

      <!-- 登录审计 -->
      <template v-else-if="tab === 'logins'">
        <div class="sec-head">
          <span class="text-dim">
            SSH 登录记录：成功 <b class="ok-text">{{ logins.accepted }}</b> 次 ·
            失败/非法用户 <b class="bad-text">{{ logins.failed }}</b> 次
          </span>
          <UIButton type="ghost" @click="loadLogins">刷新</UIButton>
        </div>
        <UITable :columns="loginCols" :rows="logins.items" :loading="loginLoading">
          <template #col-ok="{ row }">
            <UITag :tone="row.ok ? 'ok' : 'danger'">{{ row.ok ? '成功' : '失败' }}</UITag>
          </template>
          <template #col-line="{ row }"><span class="mono log-cell">{{ row.line }}</span></template>
        </UITable>
        <div class="panel mt">
          <div class="panel-head"><h2 class="card-title">本机账户</h2></div>
          <div v-if="!accounts.length" class="text-dim pad">暂无数据</div>
          <div v-for="a in accounts" :key="a.user" class="acc-row">
            <span class="mono strong">{{ a.user }}</span>
            <span class="text-dim mono">uid={{ a.uid }} gid={{ a.gid }}</span>
            <span class="mono text-dim">{{ a.shell }}</span>
            <UITag :tone="a.loginable ? 'warn' : 'dim'">{{ a.loginable ? '可登录' : '禁止登录' }}</UITag>
          </div>
        </div>
      </template>

      <ConfirmDialog
        v-model:open="act.open"
        :message="`确认对 ${act.unit} 执行「${actText}」？这会立即影响该服务。`"
        :confirm-text="actText"
        danger
        :loading="act.loading"
        @confirm="doAction(act.unit, act.action)"
      />
    </template>
  </div>
</template>

<style scoped>
.tabs { margin-bottom: 14px; }
.no-node-tip {
  background: var(--card-bg); border: 1px dashed var(--line); border-radius: var(--radius);
  padding: 26px; text-align: center; color: var(--text-dim); font-size: 13px; margin-bottom: 16px;
}
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; gap: 12px; flex-wrap: wrap; }
.row-gap { display: flex; align-items: center; gap: 8px; }
.w110 { width: 110px; }
.w120 { width: 120px; }
.w150 { width: 150px; }
.w220 { width: 220px; }
.w260 { width: 260px; }

.panel {
  background: var(--card-bg); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 16px 18px;
}
.panel.mt { margin-top: 12px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.card-title { font-size: 14px; font-weight: 700; margin: 0; }
.pad { padding: 8px 0; font-size: 13px; }

.cmd-cell { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.desc-cell { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.log-cell { font-size: 12px; word-break: break-all; }
.hot { color: #e05260; font-weight: 600; }
.strong { font-weight: 600; }
.mono.strong { font-weight: 600; }

.log-box {
  background: #0c1613; border-radius: var(--radius); padding: 14px 16px;
  max-height: 460px; overflow: auto; border: 1px solid #14231f;
}
.log-box.loading { opacity: .55; }
.log-line { color: #b9d8cf; font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-all; }
.err-box {
  background: #fdecee; border: 1px solid #f7c1c1; color: #a32d2d;
  border-radius: var(--radius); padding: 10px 14px; font-size: 13px; margin-bottom: 12px; word-break: break-all;
}
.ok-text { color: #1f8a79; }
.bad-text { color: #e05260; }
.acc-row {
  display: flex; align-items: center; gap: 16px; padding: 9px 0;
  border-bottom: 1px dashed var(--line); font-size: 13px;
}
.acc-row:last-child { border-bottom: none; }
</style>
