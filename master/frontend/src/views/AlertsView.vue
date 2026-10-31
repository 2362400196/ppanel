<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITabs from '../components/ui/UITabs.vue'
import UITable from '../components/ui/UITable.vue'
import UITag from '../components/ui/UITag.vue'
import UIInput from '../components/ui/UIInput.vue'
import UISelect from '../components/ui/UISelect.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'

const tabs = [
  { key: 'list', label: '告警列表' },
  { key: 'rules', label: '规则与阈值' },
  { key: 'notify', label: '通知渠道' }
]
const tab = ref('list')

// ---------- 汇总 ----------
const summary = ref({ total: 0, by_level: { critical: 0, warning: 0, info: 0 } })

async function loadSummary() {
  try {
    const { data } = await api.get('/admin/alerts/summary')
    summary.value = data
  } catch (e) { /* 汇总失败不打扰用户 */ }
}

// ---------- 列表 ----------
const status = ref('active')
const levelFilter = ref('')
const alerts = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const statusOptions = [
  { value: 'active', label: '未恢复' },
  { value: 'firing', label: '触发中' },
  { value: 'acked', label: '已确认' },
  { value: 'resolved', label: '已恢复' },
  { value: 'all', label: '全部' }
]
const levelOptions = [
  { value: '', label: '全部级别' },
  { value: 'critical', label: '紧急' },
  { value: 'warning', label: '警告' },
  { value: 'info', label: '提示' }
]

const columns = [
  { key: 'level', label: '级别', width: '72px' },
  { key: 'info', label: '告警' },
  { key: 'target', label: '对象', width: '150px' },
  { key: 'metric', label: '实测 / 阈值', width: '130px' },
  { key: 'fired_at', label: '触发时间', width: '132px' },
  { key: 'status', label: '状态', width: '80px' },
  { key: 'actions', label: '操作', width: '150px' }
]

async function loadAlerts() {
  loading.value = true
  try {
    const params = { status: status.value, limit: pageSize.value, offset: (page.value - 1) * pageSize.value }
    if (levelFilter.value) params.level = levelFilter.value
    const { data } = await api.get('/admin/alerts', { params })
    alerts.value = data.items
    total.value = data.total
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}

function onPageChange() { loadAlerts() }
function onFilter() { page.value = 1; loadAlerts() }

const levelTone = l => (l === 'critical' ? 'danger' : l === 'warning' ? 'warn' : 'primary')
const statusTone = s => (s === 'firing' ? 'danger' : s === 'acked' ? 'warn' : 'ok')
const statusText = s => ({ firing: '触发中', acked: '已确认', resolved: '已恢复' }[s] || s)

function fmtTs(s) {
  if (!s) return '—'
  const d = new Date(s.endsWith('Z') ? s : s + 'Z')
  if (isNaN(d.getTime())) return s
  const p = x => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function targetText(row) {
  if (row.scope === 'instance') return row.instance_name || (row.instance_uuid || '').slice(0, 8)
  return row.node_name || (row.node_id ? `节点 #${row.node_id}` : '—')
}

// ---------- 操作 ----------
const acting = ref(0)
async function doAck(row) {
  acting.value = row.id
  try {
    await api.post(`/admin/alerts/${row.id}/ack`)
    toastOk('已确认，恢复后会自动结束')
    await refresh()
  } catch (e) { toastErr(errText(e)) } finally { acting.value = 0 }
}

async function doResolve(row) {
  acting.value = row.id
  try {
    await api.post(`/admin/alerts/${row.id}/resolve`)
    toastOk('已标记恢复')
    await refresh()
  } catch (e) { toastErr(errText(e)) } finally { acting.value = 0 }
}

const delOpen = ref(false)
const delTarget = ref(null)
async function confirmDelete() {
  try {
    await api.delete(`/admin/alerts/${delTarget.value.id}`)
    toastOk('已删除')
    delOpen.value = false
    await refresh()
  } catch (e) { toastErr(errText(e)) }
}
function askDelete(row) { delTarget.value = row; delOpen.value = true }

async function refresh() {
  await Promise.all([loadSummary(), loadAlerts()])
}

// ---------- 配置 ----------
const cfg = ref(null)
const saving = ref(false)
const testing = ref(false)

const webhookTypes = [
  { value: 'generic', label: '通用 JSON' },
  { value: 'dingtalk', label: '钉钉机器人' },
  { value: 'wechat', label: '企业微信机器人' },
  { value: 'feishu', label: '飞书机器人' }
]
const enabledOptions = [{ value: true, label: '启用' }, { value: false, label: '停用' }]
const sslOptions = [{ value: true, label: 'SSL（465）' }, { value: false, label: 'STARTTLS（587）' }]

async function loadConfig() {
  try {
    const { data } = await api.get('/admin/alerts/config')
    cfg.value = { ...data, smtp_pass: '' }
  } catch (e) { toastErr(errText(e)) }
}

const numKeys = ['disk_threshold', 'disk_critical', 'mem_threshold', 'mem_critical',
  'offline_fail_count', 'latency_threshold_ms', 'crash_window_minutes',
  'crash_count', 'silence_minutes', 'smtp_port']

async function saveConfig() {
  if (!cfg.value) return
  saving.value = true
  try {
    const payload = {}
    for (const [k, v] of Object.entries(cfg.value)) {
      if (k === 'smtp_pass_set' || k === 'smtp_pass') continue
      payload[k] = numKeys.includes(k) ? Number(v) : v
    }
    if (cfg.value.smtp_pass) payload.smtp_pass = cfg.value.smtp_pass
    if (Number(payload.disk_critical) < Number(payload.disk_threshold)) {
      toastErr('磁盘紧急阈值不能低于警告阈值'); saving.value = false; return
    }
    if (Number(payload.mem_critical) < Number(payload.mem_threshold)) {
      toastErr('内存紧急阈值不能低于警告阈值'); saving.value = false; return
    }
    const { data } = await api.put('/admin/alerts/config', payload)
    cfg.value = { ...data, smtp_pass: '' }
    toastOk('配置已保存')
    await loadSummary()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    saving.value = false
  }
}

async function sendTest() {
  testing.value = true
  try {
    const { data } = await api.post('/admin/alerts/test')
    toastOk(data.detail || '测试通知已发送')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    testing.value = false
  }
}

const notifyReady = computed(() => {
  if (!cfg.value) return false
  const hook = (cfg.value.webhook_url || '').trim()
  const mail = (cfg.value.email_to || '').trim() && (cfg.value.smtp_host || '').trim()
  return Boolean(hook || mail)
})

// ---------- 生命周期 ----------
let timer = null
onMounted(async () => {
  await Promise.all([loadSummary(), loadAlerts(), loadConfig()])
  timer = setInterval(() => {
    if (tab.value === 'list') refresh()
    else loadSummary()
  }, 30000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">告警中心</h1>
        <p class="page-sub">节点离线、资源水位、服务异常与实例崩溃的集中告警，支持 Webhook 与邮件通知</p>
      </div>
      <div class="head-right">
        <UITag v-if="summary.by_level.critical" tone="danger">紧急 {{ summary.by_level.critical }}</UITag>
        <UITag v-if="summary.by_level.warning" tone="warn">警告 {{ summary.by_level.warning }}</UITag>
        <UITag v-if="!summary.total" tone="ok">全部正常</UITag>
        <UIButton type="ghost" @click="refresh">刷新</UIButton>
      </div>
    </div>

    <UITabs v-model="tab" :tabs="tabs" class="tabs" />

    <!-- 告警列表 -->
    <template v-if="tab === 'list'">
      <div class="sec-head">
        <div class="row-gap">
          <UISelect v-model="status" :options="statusOptions" class="w130" @update:model-value="onFilter" />
          <UISelect v-model="levelFilter" :options="levelOptions" class="w130" @update:model-value="onFilter" />
          <span class="dim-text">共 {{ total }} 条</span>
        </div>
        <UIButton type="ghost" :loading="loading" @click="loadAlerts">刷新列表</UIButton>
      </div>

      <UITable :columns="columns" :rows="alerts" :loading="loading"
               :total="total" v-model:page="page" v-model:page-size="pageSize"
               @change="onPageChange">
        <template #col-level="{ row }">
          <UITag :tone="levelTone(row.level)">{{ row.level_label }}</UITag>
        </template>
        <template #col-info="{ row }">
          <div class="cell-title">{{ row.title }}</div>
          <div class="cell-detail">{{ row.detail }}</div>
        </template>
        <template #col-target="{ row }">
          <span class="mono">{{ targetText(row) }}</span>
        </template>
        <template #col-metric="{ row }">
          <span v-if="row.value === null && row.threshold === null" class="dim-text">—</span>
          <span v-else class="mono">{{ row.value ?? '-' }} / {{ row.threshold ?? '-' }}</span>
        </template>
        <template #col-fired_at="{ row }">
          <span class="mono dim-text">{{ fmtTs(row.fired_at) }}</span>
        </template>
        <template #col-status="{ row }">
          <UITag :tone="statusTone(row.status)">{{ statusText(row.status) }}</UITag>
        </template>
        <template #col-actions="{ row }">
          <div class="row-gap">
            <UIButton v-if="row.status === 'firing'" type="text" :loading="acting === row.id" @click="doAck(row)">
              确认
            </UIButton>
            <UIButton v-if="row.status !== 'resolved'" type="text" :loading="acting === row.id" @click="doResolve(row)">
              恢复
            </UIButton>
            <UIButton v-if="row.status === 'resolved'" type="text" @click="askDelete(row)">删除</UIButton>
          </div>
        </template>
      </UITable>
    </template>

    <!-- 规则与阈值 -->
    <template v-if="tab === 'rules'">
      <div v-if="!cfg" class="tip-box">正在读取配置…</div>
      <template v-else>
        <div class="panel">
          <div class="panel-head">
            <h3 class="card-title">告警开关</h3>
            <UISelect v-model="cfg.enabled" :options="enabledOptions" class="w120" />
          </div>
          <p class="hint">停用后不再产生新告警，已有告警保持原状。平台内红点与历史记录不受影响。</p>
        </div>

        <div class="panel mt">
          <h3 class="card-title">资源水位</h3>
          <p class="hint">基于每次心跳采样的节点整机内存与磁盘使用率，超过警告阈值记为「警告」，超过紧急阈值记为「紧急」。</p>
          <div class="form-grid">
            <label class="fld">
              <span class="fld-k">磁盘警告阈值（%）</span>
              <UIInput v-model="cfg.disk_threshold" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">磁盘紧急阈值（%）</span>
              <UIInput v-model="cfg.disk_critical" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">内存警告阈值（%）</span>
              <UIInput v-model="cfg.mem_threshold" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">内存紧急阈值（%）</span>
              <UIInput v-model="cfg.mem_critical" type="number" />
            </label>
          </div>
        </div>

        <div class="panel mt">
          <h3 class="card-title">可用性与稳定性</h3>
          <p class="hint">心跳每 60 秒一轮。离线判定需要连续失败达到次数，避免网络抖动误报。</p>
          <div class="form-grid">
            <label class="fld">
              <span class="fld-k">离线判定：连续失败次数</span>
              <UIInput v-model="cfg.offline_fail_count" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">响应缓慢阈值（毫秒）</span>
              <UIInput v-model="cfg.latency_threshold_ms" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">崩溃统计窗口（分钟）</span>
              <UIInput v-model="cfg.crash_window_minutes" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">窗口内崩溃次数达到</span>
              <UIInput v-model="cfg.crash_count" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">未恢复时重复通知间隔（分钟）</span>
              <UIInput v-model="cfg.silence_minutes" type="number" />
            </label>
          </div>
          <p class="hint">重复通知间隔设为 0 表示每条告警只通知一次。</p>
        </div>

        <div class="panel mt">
          <UIButton :loading="saving" @click="saveConfig">保存配置</UIButton>
        </div>
      </template>
    </template>

    <!-- 通知渠道 -->
    <template v-if="tab === 'notify'">
      <div v-if="!cfg" class="tip-box">正在读取配置…</div>
      <template v-else>
        <div class="panel">
          <h3 class="card-title">Webhook</h3>
          <p class="hint">
            通用 JSON 适合自建接收端；钉钉 / 企业微信 / 飞书 机器人会自动套用对应消息格式。
            钉钉与飞书若开启了「加签」，把密钥填到签名密钥即可。
          </p>
          <div class="form-grid">
            <label class="fld span2">
              <span class="fld-k">Webhook 地址</span>
              <UIInput v-model="cfg.webhook_url" placeholder="https://..." />
            </label>
            <label class="fld">
              <span class="fld-k">消息格式</span>
              <UISelect v-model="cfg.webhook_type" :options="webhookTypes" />
            </label>
            <label class="fld">
              <span class="fld-k">签名密钥（可选）</span>
              <UIInput v-model="cfg.webhook_secret" placeholder="加签 secret" />
            </label>
          </div>
        </div>

        <div class="panel mt">
          <h3 class="card-title">邮件</h3>
          <p class="hint">SMTP 发送。密码留空表示保持已保存的密码不变。</p>
          <div class="form-grid">
            <label class="fld span2">
              <span class="fld-k">收件人（多个用逗号分隔）</span>
              <UIInput v-model="cfg.email_to" placeholder="ops@example.com" />
            </label>
            <label class="fld">
              <span class="fld-k">SMTP 服务器</span>
              <UIInput v-model="cfg.smtp_host" placeholder="smtp.example.com" />
            </label>
            <label class="fld">
              <span class="fld-k">端口</span>
              <UIInput v-model="cfg.smtp_port" type="number" />
            </label>
            <label class="fld">
              <span class="fld-k">登录账号</span>
              <UIInput v-model="cfg.smtp_user" placeholder="no-reply@example.com" />
            </label>
            <label class="fld">
              <span class="fld-k">密码 {{ cfg.smtp_pass_set ? '（已设置，留空不改）' : '' }}</span>
              <UIInput v-model="cfg.smtp_pass" type="password" placeholder="••••••" />
            </label>
            <label class="fld">
              <span class="fld-k">加密方式</span>
              <UISelect v-model="cfg.smtp_ssl" :options="sslOptions" />
            </label>
          </div>
        </div>

        <div class="panel mt">
          <div class="row-gap">
            <UIButton :loading="saving" @click="saveConfig">保存配置</UIButton>
            <UIButton type="ghost" :loading="testing" :disabled="!notifyReady" @click="sendTest">
              发送测试通知
            </UIButton>
            <span v-if="!notifyReady" class="dim-text">填写 Webhook 或完整邮件配置后可发送测试</span>
          </div>
        </div>
      </template>
    </template>

    <ConfirmDialog v-model:open="delOpen" title="删除告警记录" danger confirm-text="删除"
                   :message="delTarget ? `确认删除这条已恢复的告警记录？删除后不可恢复。` : ''"
                   @confirm="confirmDelete" />
  </div>
</template>

<style scoped>
.tabs { margin-bottom: 18px; }
.head-right { display: flex; align-items: center; gap: 8px; }
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; gap: 12px; flex-wrap: wrap; }
.row-gap { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.dim-text { font-size: 12px; color: var(--text-dim); }
.w120 { width: 120px; }
.w130 { width: 130px; }
.mono { font-family: var(--font-mono, monospace); font-size: 12px; }

.cell-title { font-size: 13px; color: var(--text-1); line-height: 1.5; }
.cell-detail { font-size: 12px; color: var(--text-dim); line-height: 1.5; margin-top: 2px; }

.tip-box {
  background: var(--card-bg); border: 1px dashed var(--line); border-radius: var(--radius);
  padding: 26px; text-align: center; color: var(--text-dim); font-size: 13px;
}
.panel {
  background: var(--card-bg); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 16px 18px;
}
.panel.mt { margin-top: 12px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.card-title { font-size: 14px; font-weight: 700; margin: 0 0 6px; }
.hint { font-size: 12px; color: var(--text-dim); line-height: 1.7; margin: 6px 0 14px; }

.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; }
.fld { display: flex; flex-direction: column; gap: 6px; }
.fld.span2 { grid-column: span 2; }
.fld-k { font-size: 12px; color: var(--text-dim); }

@media (max-width: 760px) {
  .form-grid { grid-template-columns: 1fr; }
  .fld.span2 { grid-column: span 1; }
}
</style>
