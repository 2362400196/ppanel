<template>
  <div>
    <h1 class="page-title">备份中心</h1>
    <p class="page-sub">统一管理所有节点的定时备份与备份文件，无需进入节点管理面板</p>

    <!-- 节点选择：任务 / 文件 / 存储配置均按节点隔离 -->
    <div class="node-bar">
      <template v-if="nodes.length">
        <span class="node-lbl">备份节点</span>
        <UISelect v-model="curId" :options="nodeOpts" style="width:240px" />
        <template v-if="curNode">
          <StatusDot :status="curNode.online ? 'running' : 'exited'" />
          <span class="hint-text">{{ curNode.online ? '在线' : '离线' }}</span>
        </template>
      </template>
      <span v-if="!nodes.length" class="hint-text">暂无节点，请先到「节点管理」添加</span>
    </div>

    <template v-if="curId">
      <!-- 定时备份任务 -->
      <div class="card">
        <div class="bar">
          <h3 class="card-title">定时备份</h3>
          <span class="sp" />
          <UIButton type="ghost" size="sm" :loading="bjLoading" @click="loadAll">刷新</UIButton>
          <UIButton size="sm" @click="bjOpen = !bjOpen">{{ bjOpen ? '收起' : '新建任务' }}</UIButton>
        </div>

        <div v-if="bjOpen" class="form">
          <div class="row">
            <span class="lbl">周期</span>
            <UIInput v-model="bjSchedule" style="width:190px" class="mono"
                     placeholder="cron：分 时 日 月 周" />
            <span v-for="p in PRESETS" :key="p.value" class="chip"
                  :class="{ on: bjSchedule === p.value }" @click="bjSchedule = p.value">{{ p.label }}</span>
          </div>
          <div class="row">
            <span class="lbl">类型</span>
            <UISelect v-model="bjKind" style="width:170px" :options="kindOpts" />
            <UISelect v-if="bjKind === 'container' || bjKind === 'dir'"
                      v-model="bjContainer" style="width:250px"
                      :options="containerOpts" placeholder="选择容器" />
            <UISelect v-else v-model="bjDb" style="width:250px"
                      :options="dbOpts" placeholder="选择数据库" />
            <UIInput v-if="bjKind === 'dir'" v-model="bjDirPath" style="width:170px" class="mono"
                     placeholder="容器内目录，默认 /app" />
          </div>
          <div v-if="bjKind === 'db_table' && bjDb" class="row sub">
            <template v-if="tableOpts.length">
              <span class="hint-text">选择单表（不选 = 整库）：</span>
              <span v-for="t in tableOpts" :key="t" class="chip"
                    :class="{ on: bjTable === t }" @click="bjTable = bjTable === t ? '' : t">{{ t }}</span>
            </template>
            <span v-else class="hint-text">该库暂无数据表，可改用整库备份</span>
          </div>
          <div class="row">
            <span class="lbl">目的地</span>
            <UISelect v-model="bjDest" style="width:200px" :options="destOpts" />
            <span v-if="bjDest !== 'local' && !cosReady" class="hint-text warn-text">
              COS 未启用，请先在下方「存储设置」保存并启用</span>
          </div>
          <div class="row">
            <span class="lbl">保留</span>
            <UIInput v-model.number="bjKeep" style="width:80px" placeholder="5" />
            <span class="hint-text">份（超出自动删最旧）</span>
            <UIButton size="sm" :loading="bjBusy" :disabled="!canSave" @click="saveJob">创建并跑首份</UIButton>
          </div>
          <p class="tip">按周期自动备份，到点在被控后台执行；整库与表级任务互不干扰、各算各的保留份数。
            目的地选「腾讯云 COS」时备份自动上传云端，可配置是否保留本地副本。</p>
        </div>

        <table class="ftable">
          <thead><tr><th>周期</th><th>类型</th><th>目标</th><th>最近执行</th><th /></tr></thead>
          <tbody>
            <tr v-for="j in jobs" :key="j.id">
              <td class="mono">{{ schedLabel(j.schedule) }}</td>
              <td><UITag tone="primary">{{ KIND_LABEL[j.kind] || j.kind }}</UITag></td>
              <td class="mono">{{ targetLabel(j) }}</td>
              <td>
                <UITag :tone="j.last_status === 'ok' ? 'ok' : j.last_status === 'fail' ? 'warn' : j.last_status === 'running' ? 'primary' : 'dim'">
                  {{ j.last_status === 'ok' ? '成功' : j.last_status === 'fail' ? '失败' : j.last_status === 'running' ? '执行中' : '未执行' }}
                </UITag>
                <span class="dim" style="font-size:12px;margin-left:6px">{{ fmtISO(j.last_run) }}</span>
                <div v-if="j.last_output" class="hint-text" style="font-size:12px">{{ j.last_output.slice(0, 70) }}</div>
              </td>
              <td class="ops">
                <UIButton type="text" @click="toggleJob(j)">{{ j.enabled ? '暂停' : '启用' }}</UIButton>
                <UIButton type="text" @click="runJob(j)">立即执行</UIButton>
                <UIButton type="text" class="danger" @click="delJob(j)">删除</UIButton>
              </td>
            </tr>
            <tr v-if="!jobs"><td colspan="5" class="empty">暂无定时任务，点击「新建任务」创建</td></tr>
          </tbody>
        </table>
      </div>

      <!-- 存储设置（腾讯云 COS） -->
      <div class="card">
        <div class="bar">
          <h3 class="card-title">存储设置</h3>
          <UITag :tone="cos.enabled ? 'ok' : 'dim'">
            {{ cos.enabled ? `COS 已启用（${cos.bucket}）` : '仅本地存储' }}</UITag>
          <span class="sp" />
          <span class="hint-text">{{ cos.enabled ? '定时备份可上传到腾讯云 COS' : '当前所有备份仅存节点本地' }}</span>
          <UIButton size="sm" @click="openCos">配置存储桶</UIButton>
        </div>
      </div>

      <!-- 备份文件 -->
      <div class="card">
        <div class="bar">
          <h3 class="card-title">备份文件</h3>
          <span class="sp" />
          <span class="hint-text">共 {{ bkTotal }} 个（保留策略之外的旧文件可在此手动清理）</span>
          <UIButton type="ghost" size="sm" :loading="bkLoading" @click="loadFiles">刷新</UIButton>
        </div>
        <table class="ftable">
          <thead><tr><th>文件名</th><th>类型</th><th>大小</th><th>时间</th><th /></tr></thead>
          <tbody>
            <tr v-for="b in backups" :key="b.file">
              <td class="mono">{{ b.file }}</td>
              <td>
                <UITag :tone="b.kind === 'container' ? 'primary' : b.kind === 'database' ? 'ok' : b.kind === 'dir' ? 'warn' : 'dim'">
                  {{ b.kind === 'container' ? '容器' : b.kind === 'database' ? '数据库' : b.kind === 'dir' ? '目录' : '其他' }}
                </UITag>
              </td>
              <td class="mono">{{ fmtSize(b.size) }}</td>
              <td class="dim">{{ b.created_at }}</td>
              <td class="ops">
                <UIButton type="text" @click="downloadFile(b.file)">下载</UIButton>
                <UIButton type="text" class="danger" @click="askDelFile(b.file)">删除</UIButton>
              </td>
            </tr>
            <tr v-if="!backups"><td colspan="5" class="empty">暂无备份文件</td></tr>
          </tbody>
        </table>
        <UIPager v-if="bkTotal > 0" class="ft-pager"
                 :total="bkTotal" v-model:page="bkPage" v-model:pageSize="bkPageSize"
                 @change="loadFiles" />
        <p class="tip">恢复操作（导入容器 / 覆盖导入数据库）在「节点管理 → 备份」中执行，防止误覆盖线上数据。</p>
      </div>
    </template>

    <ConfirmDialog v-model:open="delOpen" title="删除备份"
                   :message="`确定删除备份文件「${delFile_}」吗？此操作不可恢复。`"
                   confirm-text="删除" @confirm="doDelFile" />

    <!-- 存储桶配置弹窗 -->
    <UIModal v-model:open="cosOpen" width="620px" title="配置存储桶（腾讯云 COS）">
      <div class="form">
        <div class="row">
          <span class="lbl">SecretId</span>
          <UIInput v-model="cos.secret_id" style="width:100%" class="mono"
                   :placeholder="cos.has_key ? cos.secret_id || '（沿用已保存）' : '腾讯云 API 密钥 SecretId'" />
        </div>
        <div class="row">
          <span class="lbl">SecretKey</span>
          <UIInput v-model="cos.secret_key" style="width:100%" class="mono" type="password"
                   :placeholder="cos.has_key ? `已保存（${cos.secret_key_masked}），留空沿用` : '腾讯云 API 密钥 SecretKey'" />
        </div>
        <div class="row">
          <span class="lbl">存储桶</span>
          <UIInput v-model="cos.bucket" style="width:220px" class="mono" placeholder="例如 mybk-1250000000" />
          <span class="lbl">地域</span>
          <UIInput v-model="cos.region" style="width:200px" class="mono" placeholder="ap-guangzhou" />
        </div>
        <div class="row">
          <span class="lbl">前缀</span>
          <UIInput v-model="cos.prefix" style="width:220px" class="mono" placeholder="ppanel-backups" />
        </div>
        <div class="row">
          <label class="chk"><input type="checkbox" v-model="cos.keep_local"> 上传后保留本地副本</label>
          <label class="chk"><input type="checkbox" v-model="cos.enabled"> 启用 COS 云备份</label>
        </div>
        <p class="tip">密钥在腾讯云控制台「访问管理 → API 密钥」创建；建议使用仅授予该桶读写权限的子账号密钥。
          对象上传到 &lt;前缀&gt;/&lt;备份文件名&gt;。{{ cos.enabled ? '保存时自动校验连通性，校验失败将保持未启用。' : '未启用时所有备份仅存本地。' }}</p>
        <div class="row" style="justify-content:flex-end">
          <UIButton type="ghost" size="sm" @click="cosOpen = false">取消</UIButton>
          <UIButton size="sm" :loading="cosBusy" @click="saveCos">保存{{ cos.enabled ? '并校验' : '' }}</UIButton>
        </div>
      </div>
    </UIModal>
  </div>
</template>

<script setup>
// 备份中心：跨节点统一管理定时备份任务与备份文件（/api/host/* 透传被控）
import { computed, ref, watch } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UIModal from '../components/ui/UIModal.vue'
import UISelect from '../components/ui/UISelect.vue'
import UIPager from '../components/ui/UIPager.vue'
import UITag from '../components/ui/UITag.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import { startTask } from '../api/tasks'

const nodes = ref([])
const curId = ref('')
const np = () => ({ node: curId.value })

// ---------- 定时任务 ----------
const jobs = ref([])
const bjLoading = ref(false)
const bjOpen = ref(false)
const bjSchedule = ref('0 3 * * *')
const bjKind = ref('db_full')
const bjContainer = ref('')
const bjDirPath = ref('/app')
const bjDb = ref('')
const bjTable = ref('')
const tableOpts = ref([])
const bjKeep = ref(5)
const bjDest = ref('local')
const bjBusy = ref(false)
const meta = ref({ containers: [], mysql: [] })

// ---------- COS 存储设置 ----------
const cos = ref({ secret_id: '', secret_key: '', secret_key_masked: '', has_key: false,
                  bucket: '', region: '', prefix: 'ppanel-backups', keep_local: true, enabled: false })
const cosBusy = ref(false)
const cosReady = computed(() => cos.value.enabled)
const destOpts = [
  { value: 'local', label: '本地存储' },
  { value: 'cos', label: '腾讯云 COS' },
  { value: 'both', label: '本地 + 腾讯云 COS' },
]

async function loadCos() {
  try {
    const { data } = await api.get('/host/cos', { params: np() })
    cos.value = { ...data, secret_key: '' }
  } catch (e) {
    const s = e?.response?.status
    if (s !== 404) toastErr(errText(e))
  }
}

const cosOpen = ref(false)

function openCos() {
  cos.value.secret_key = ''  // 每次打开清空密钥输入（留空 = 沿用）
  loadCos()
  cosOpen.value = true
}

async function saveCos() {
  cosBusy.value = true
  try {
    const { data } = await api.put('/host/cos', {
      secret_id: cos.value.secret_id, secret_key: cos.value.secret_key,
      bucket: cos.value.bucket, region: cos.value.region, prefix: cos.value.prefix,
      keep_local: cos.value.keep_local ? 1 : 0, enabled: cos.value.enabled ? 1 : 0,
    }, { params: np() })
    toastOk(data.message || '已保存')
    cosOpen.value = false
    loadCos()
  } catch (e) { toastErr(errText(e)) }
  finally { cosBusy.value = false }
}

const KIND_LABEL = { container: '容器', dir: '目录', db_full: '数据库整库', db_table: '数据库表级' }
const PRESETS = [
  { value: '0 3 * * *', label: '每天 03:00' },
  { value: '0 4 * * 1', label: '每周一 04:00' },
  { value: '0 */6 * * *', label: '每 6 小时' },
  { value: '30 2 1 * *', label: '每月 1 日 02:30' },
]
const kindOpts = Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))
const containerOpts = computed(() => (meta.value.containers || [])
  .map(c => ({ value: c.name, label: `${c.name}（${c.image}${c.status === 'running' ? '' : '，已停止'}）` })))
const dbOpts = computed(() => (meta.value.mysql || []).filter(m => m.running).flatMap(m =>
  m.dbs.map(d => ({ value: `${m.version}|${d}`, label: `${d}（MySQL ${m.version}）` }))))
const canSave = computed(() => {
  if (!bjSchedule.value.trim()) return false
  if (bjKind.value === 'container' || bjKind.value === 'dir') return !!bjContainer.value
  return !!bjDb.value
})
const schedLabel = s => (PRESETS.find(p => p.value === s) || {}).label || s
const targetLabel = j => {
  if (j.kind === 'container' || j.kind === 'dir') return `${j.target}:${j.dir_path || '/app'}`
  return j.kind === 'db_table' && j.table_name ? `${j.target.split(':')[1]}.${j.table_name}` : j.target.replace(':', ' / ')
}
function fmtISO(s) {
  if (!s) return ''
  const d = new Date(s.endsWith('Z') ? s : s + 'Z')
  const p = x => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function loadJobs() {
  bjLoading.value = true
  try {
    const [jobsR, metaR] = await Promise.all([
      api.get('/host/backup-jobs', { params: np() }),
      api.get('/host/backup-jobs/meta', { params: np() }).catch(() => null),
    ])
    jobs.value = jobsR.data.jobs || []
    if (metaR) meta.value = metaR.data
  } catch (e) {
    const s = e?.response?.status
    if (s !== 404) toastErr(errText(e))
    jobs.value = []
  } finally { bjLoading.value = false }
}

watch(bjDb, async v => {
  bjTable.value = ''
  tableOpts.value = []
  if (!v || !curId.value) return
  const [version, db_name] = v.split('|')
  try {
    const { data } = await api.get('/host/backups/tables', { params: { ...np(), version, db_name } })
    tableOpts.value = data.tables || []
  } catch { tableOpts.value = [] }
})

async function saveJob() {
  bjBusy.value = true
  try {
    const payload = { schedule: bjSchedule.value.trim(), kind: bjKind.value, keep: bjKeep.value || 5,
                      dest: bjDest.value }
    if (bjKind.value === 'container' || bjKind.value === 'dir') {
      payload.target = bjContainer.value
      payload.dir_path = bjDirPath.value.trim() || '/app'
    } else {
      const [version, db_name] = bjDb.value.split('|')
      payload.target = `${version}:${db_name}`
      payload.table_name = bjKind.value === 'db_table' ? bjTable.value : ''
    }
    const { data } = await api.post('/host/backup-jobs', payload, { params: np() })
    toastOk(`定时任务已创建（${KIND_LABEL[data.kind]}），首份备份执行中`)
    bjOpen.value = false
    loadJobs(); loadFiles()
  } catch (e) { toastErr(errText(e)) }
  finally { bjBusy.value = false }
}

async function toggleJob(j) {
  try {
    await api.patch(`/host/backup-jobs/${j.id}`, { enabled: !j.enabled }, { params: np() })
    j.enabled = !j.enabled
    toastOk(j.enabled ? '任务已启用' : '任务已暂停')
  } catch (e) { toastErr(errText(e)) }
}

async function runJob(j) {
  try {
    const tid = startTask(curId.value)
    const { data } = await api.post(`/host/backup-jobs/${j.id}/run`, {},
      { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '执行完成')
    loadJobs(); loadFiles()
  } catch (e) { toastErr(errText(e)); loadJobs() }
}

async function delJob(j) {
  try {
    await api.delete(`/host/backup-jobs/${j.id}`, { params: np() })
    toastOk('定时任务已删除')
    loadJobs()
  } catch (e) { toastErr(errText(e)) }
}

// ---------- 备份文件 ----------
const backups = ref([])
const bkTotal = ref(0)
const bkPage = ref(1)
const bkPageSize = ref(20)
const bkLoading = ref(false)

async function loadFiles() {
  bkLoading.value = true
  try {
    const { data } = await api.get('/host/backups', {
      params: { ...np(), page: bkPage.value, page_size: bkPageSize.value }
    })
    backups.value = data.backups || []
    bkTotal.value = data.total || 0
    // 删除后当前页空了 → 回退到最后一页
    if (!backups.value.length && bkPage.value > 1 && bkTotal.value > 0) {
      bkPage.value = Math.ceil(bkTotal.value / bkPageSize.value)
      return loadFiles()
    }
  } catch (e) {
    const s = e?.response?.status
    if (s !== 404) toastErr(errText(e))
    backups.value = []
  } finally { bkLoading.value = false }
}

async function downloadFile(file) {
  try {
    const { data } = await api.get('/host/backups/download',
      { params: { ...np(), file }, responseType: 'blob' })
    const url = URL.createObjectURL(data)
    const a = document.createElement('a')
    a.href = url
    a.download = file
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) { toastErr(errText(e)) }
}

const delOpen = ref(false)
const delFile_ = ref('')

function askDelFile(file) { delFile_.value = file; delOpen.value = true }

async function doDelFile() {
  const file = delFile_.value
  delOpen.value = false
  try {
    await api.delete('/host/backups', { params: { ...np(), file } })
    toastOk('已删除')
    loadFiles()
  } catch (e) { toastErr(errText(e)) }
}

function fmtSize(n) {
  if (n === '' || n == null) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`
  return `${(n / 1073741824).toFixed(2)} GB`
}

// ---------- 节点选择 ----------
// 任务 / 备份文件 / COS 配置都按节点隔离；节点多时走下拉，不再受横排 chip 挤爆
const nodeOpts = computed(() => nodes.value.map(n => ({
  value: String(n.id), label: n.online ? n.name : `${n.name}（离线）`
})))
const curNode = computed(() => nodes.value.find(n => String(n.id) === curId.value) || null)

watch(curId, (v, old) => {
  if (!v) return
  const n = nodes.value.find(x => String(x.id) === v)
  if (n && !n.online) {
    toastErr(`节点「${n.name}」离线，无法管理备份`)
    curId.value = old  // 回退到原节点
    return
  }
  jobs.value = []
  backups.value = []
  bkPage.value = 1
  meta.value = { containers: [], mysql: [] }
  loadJobs(); loadFiles(); loadCos()
})

async function loadNodes() {
  try {
    const { data } = await api.get('/admin/nodes')
    nodes.value = data || []
    if (!curId.value && nodes.value.length) {
      const first = nodes.value.find(n => n.online) || nodes.value[0]
      curId.value = String(first.id)  // watch 自动触发加载
    }
  } catch (e) { toastErr(errText(e)) }
}

function loadAll() { loadJobs(); loadFiles() }

loadNodes()
</script>

<style scoped>
/* 页面宽度跟随 main-body，与其他页面统一（不再 max-width 居中留白） */
.node-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 14px 0 16px; }
.node-lbl { font-size: 13px; color: var(--text-dim); }
.card {
  border: 1px solid var(--line); border-radius: 12px;
  padding: 14px 16px; margin-bottom: 16px; background: var(--panel, transparent);
}
.bar { display: flex; align-items: center; gap: 10px; }
.card-title { font-size: 15px; font-weight: 600; margin: 0; }
.sp { flex: 1; }
.form { margin-top: 12px; display: flex; flex-direction: column; gap: 10px; }
.row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.row.sub { padding-left: 52px; }
.lbl { font-size: 13px; color: var(--text-dim); width: 42px; flex: none; }
.chip {
  padding: 3px 10px; border-radius: 999px; font-size: 12px;
  border: 1px solid var(--line); background: transparent; color: var(--text-dim);
  cursor: pointer; transition: all .15s;
}
.chip:hover { border-color: var(--primary); color: var(--text); }
.chip.on { background: color-mix(in srgb, var(--primary) 14%, transparent); border-color: var(--primary); color: var(--text); }
.tip { font-size: 12px; color: var(--text-dim); margin: 8px 0 0; line-height: 1.6; }
.hint-text { font-size: 12px; color: var(--text-dim); }
.warn-text { color: #e5484d; }
.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
.chk input { accent-color: var(--primary); }
.dim { color: var(--text-dim); }
.mono { font-family: ui-monospace, Consolas, monospace; }
.ops { text-align: right; white-space: nowrap; }
.empty { text-align: center; color: var(--text-dim); padding: 18px 0 !important; }
.ft-pager { justify-content: flex-end; margin-top: 10px; }
table.ftable { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }
.ftable th { text-align: left; padding: 8px 10px; font-size: 12px; color: var(--text-dim); border-bottom: 1px solid var(--line); font-weight: 500; }
.ftable td { padding: 7px 10px; border-bottom: 1px solid color-mix(in srgb, var(--line) 55%, transparent); vertical-align: middle; }
.ftable tbody tr:hover td { background: color-mix(in srgb, var(--primary) 4%, transparent); }
.danger { color: var(--danger, #e5484d); }
</style>
