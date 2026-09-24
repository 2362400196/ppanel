<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, errText, wsUrl } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITabs from '../components/ui/UITabs.vue'
import UITable from '../components/ui/UITable.vue'
import UITag from '../components/ui/UITag.vue'
import UIInput from '../components/ui/UIInput.vue'
import UISelect from '../components/ui/UISelect.vue'
import UIModal from '../components/ui/UIModal.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import Terminal from '../components/Terminal.vue'
import HostTerminal from '../components/HostTerminal.vue'

const tab = ref('containers')
const dockerDown = ref(false)

// ---------- 节点选择 ----------
const nodes = ref([])
const nodeId = ref('')
const nodeOptions = computed(() => nodes.value.map(n => ({ value: String(n.id), label: n.name + (n.online ? '' : '（离线）') })))
const curNode = computed(() => nodes.value.find(n => String(n.id) === String(nodeId.value)))
const np = () => ({ node: nodeId.value })

async function loadNodes() {
  try {
    const { data } = await api.get('/admin/nodes')
    nodes.value = data
    if (!nodeId.value && data.length) {
      nodeId.value = String(data[0].id)
    }
  } catch (e) {
    toastErr(errText(e))
  }
}

watch(nodeId, () => {
  loadContainers()
  loadImages()
  loadSettings()
})

async function guard(fn) {
  try {
    await fn()
    dockerDown.value = false
  } catch (e) {
    if (e?.response?.status === 503) dockerDown.value = true
    toastErr(errText(e))
  }
}

function fmtWhen(v) {
  if (!v) return '-'
  let t = typeof v === 'number' ? v * 1000 : Date.parse(v)
  if (Number.isNaN(t)) return String(v)
  const d = new Date(t)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ---------- 容器 ----------
const containers = ref([])
const contCols = [
  { key: 'name', label: '名称' },
  { key: 'image', label: '镜像' },
  { key: 'state', label: '状态', width: '120px' },
  { key: 'ports', label: '端口映射', width: '200px' },
  { key: 'created', label: '创建时间', width: '150px' },
  { key: 'ops', label: '', width: '210px' }
]
const runningCount = computed(() => containers.value.filter(c => c.state === 'running').length)

async function loadContainers() {
  if (!nodeId.value) return
  try {
    const { data } = await api.get('/docker/containers', { params: np() })
    containers.value = data
  } catch (e) {
    if (e?.response?.status === 503) dockerDown.value = true
  }
}

function contAction(c, action) {
  guard(async () => {
    const { data } = await api.post(`/docker/containers/${c.id}/${action}`, null, { params: np() })
    toastOk(data.detail)
    loadContainers()
  })
}

const confirmOpen = ref(false)
const confirmCtx = ref({ title: '', message: '', danger: true, run: () => {} })
function askConfirm(title, message, run) {
  confirmCtx.value = { title, message, danger: true, run }
  confirmOpen.value = true
}
function removeContainer(c, force) {
  askConfirm('删除容器', `确定删除容器「${c.name}」吗？${force ? '（强制：运行中将被先杀死）' : ''}`, async () => {
    await api.delete(`/docker/containers/${c.id}`, { params: { ...np(), ...(force ? { force: 1 } : {}) } })
    toastOk('容器已删除')
    loadContainers()
  })
}
function onRemoveClick(c) {
  if (c.state === 'running') {
    askConfirm('删除运行中的容器', `「${c.name}」正在运行，强制删除将直接杀死进程。继续吗？`, async () => {
      await api.delete(`/docker/containers/${c.id}`, { params: { ...np(), force: 1 } })
      toastOk('容器已删除')
      loadContainers()
    })
  } else {
    removeContainer(c, false)
  }
}

const logsOpen = ref(false)
const logsName = ref('')
const logsText = ref('')
async function showLogs(c) {
  logsName.value = c.name
  logsText.value = '加载中...'
  logsOpen.value = true
  guard(async () => {
    const { data } = await api.get(`/docker/containers/${c.id}/logs`, { params: { ...np(), tail: 300 } })
    logsText.value = data.logs || '(无日志)'
  })
}

// ---------- 镜像 ----------
const images = ref([])
const imgCols = [
  { key: 'full_name', label: '镜像' },
  { key: 'size', label: '大小', width: '100px' },
  { key: 'created', label: '构建时间', width: '150px' },
  { key: 'ops', label: '', width: '80px' }
]
const pullImageName = ref('')
const pulling = ref(false)
const pullTerm = ref(null)
const pullOpen = ref(false)
const pullTitle = ref('')
let pullWs = null
let pullBuf = []        // Terminal 未挂载时先缓冲，挂载后统一刷入
let pullWatchdog = null   // 链路看门狗：连接后一直无任何消息
let pullRepoTimer = null  // 仓库看门狗：agent 已响应但迟迟无拉取事件
let pullGotAny = false
let pullGotEvent = false

function flushPull() {
  if (!pullTerm.value || !pullBuf.length) return
  for (const msg of pullBuf) {
    if (msg.charCodeAt(0) === 1) pullTerm.value.redraw(msg.slice(1))  // 进度条快照：清空重绘
    else pullTerm.value.write(msg + '\n')
  }
  pullBuf = []
}
watch(pullTerm, flushPull) // Terminal 挂载瞬间刷出缓冲

function pullStop() {
  clearTimeout(pullWatchdog)
  clearTimeout(pullRepoTimer)
  pulling.value = false
}

async function loadImages() {
  if (!nodeId.value) return
  try {
    const { data } = await api.get('/docker/images', { params: np() })
    images.value = data
  } catch (e) { /* guard 一致即可 */ }
}

function startPull() {
  const image = pullImageName.value.trim()
  if (!image || pulling.value) return
  pulling.value = true
  pullTitle.value = image
  pullOpen.value = true
  pullTerm.value?.clear()
  pullBuf = [`$ docker pull ${image}`]
  nextTick(flushPull)
  pullGotAny = false
  pullGotEvent = false
  // 看门狗1：WS 打开但一条消息都没有 → 链路问题
  pullWatchdog = setTimeout(() => {
    if (pullGotAny) return
    pullBuf.push('[错误] 20 秒未收到任何拉取进度：节点可能正在重启、被控不在线或网络不通，请稍后重试')
    flushPull()
    pullWs?.close()
    pulling.value = false
  }, 20000)
  pullWs = new WebSocket(wsUrl('/ws/docker/pull', { image, node: nodeId.value }))
  pullWs.onmessage = ev => {
    pullBuf.push(ev.data)
    flushPull()
    if (!pullGotAny) {
      pullGotAny = true
      // 看门狗2：agent 已响应但 40s 仍无拉取事件 → 仓库查询缓慢或镜像名不存在
      clearTimeout(pullWatchdog)
      pullRepoTimer = setTimeout(() => {
        if (pullGotEvent) return
        pullBuf.push('[错误] 40 秒仍无拉取进度：仓库查询缓慢，或该镜像不存在。请检查镜像名格式（注意冒号分隔标签，如 php:7.4）')
        flushPull()
        pullWs?.close()
        pulling.value = false
      }, 40000)
    }
    if (!ev.data.startsWith('[agent]')) pullGotEvent = true
    if (ev.data.startsWith('[agent] 拉取完成') || ev.data.startsWith('[错误]')) {
      clearTimeout(pullWatchdog)
      clearTimeout(pullRepoTimer)
      pulling.value = false
      loadImages()
    }
  }
  pullWs.onerror = pullStop
  pullWs.onclose = pullStop
}

function removeImage(img) {
  askConfirm('删除镜像', `确定删除镜像「${img.full_name}」吗？`, async () => {
    const { data } = await api.delete(`/docker/images/${img.full_name}`, { params: np() })
    toastOk(data.detail)
    loadImages()
  })
}
function pruneImages() {
  askConfirm('清理悬空镜像', '删除所有 <none>:<none> 悬空镜像，释放磁盘空间。继续吗？', async () => {
    const { data } = await api.post('/docker/images/prune', null, { params: np() })
    toastOk(data.detail)
    loadImages()
  })
}

// ---------- 设置 ----------
const info = ref(null)
const mirrors = ref([])
const savingSettings = ref(false)

async function loadSettings() {
  if (!nodeId.value) return
  guard(async () => {
    const { data } = await api.get('/docker/settings', { params: np() })
    mirrors.value = data.registry_mirrors.map(m => ({ v: m }))
    const { data: dinfo } = await api.get('/docker/info', { params: np() })
    info.value = dinfo
  })
}
function addMirror() { mirrors.value.push({ v: '' }) }
function rmMirror(i) { mirrors.value.splice(i, 1) }

function saveSettings() {
  const list = mirrors.value.map(m => m.v.trim()).filter(Boolean)
  askConfirm(
    '保存并重启 Docker',
    '将把镜像加速地址写入 daemon.json 并重启 Docker 生效。重启期间所有容器会短暂中断（面板实例会自动拉起）。继续吗？',
    async () => {
      savingSettings.value = true
      try {
        const { data } = await api.put('/docker/settings', { registry_mirrors: list, restart: true }, { params: np() })
        toastOk(data.detail)
      } catch (e) {
        toastErr(errText(e))
      } finally {
        savingSettings.value = false
        loadSettings()
      }
    }
  )
}

const infoItems = computed(() => info.value ? [
  ['Docker 版本', info.value.server_version],
  ['操作系统', info.value.os],
  ['架构', info.value.arch],
  ['存储驱动', info.value.storage_driver],
  ['CPU 核数', info.value.cpus],
  ['内存', `${info.value.mem_total_gb} GB`],
  ['运行中容器', `${info.value.containers_running} / ${info.value.containers_running + info.value.containers_stopped + info.value.containers_paused}`],
  ['镜像数量', info.value.images],
] : [])

// ---------- 宿主机终端（HostTerminal 组件，xterm.js PTY） ----------

let timer
onMounted(() => {
  loadNodes()
  timer = setInterval(loadContainers, 8000)
})
onUnmounted(() => {
  clearInterval(timer)
  clearTimeout(pullWatchdog)
  clearTimeout(pullRepoTimer)
  pullWs?.close()
})

// 终端由 HostTerminal 组件自管理（Tab 切换时随 v-if 重建并自动重连）

const tabs = [
  { key: 'containers', label: '容器管理' },
  { key: 'images', label: '镜像管理' },
  { key: 'settings', label: 'Docker 设置' },
  { key: 'terminal', label: '宿主机终端' }
]
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">Docker 管理</h1>
        <p class="page-sub">各节点容器与镜像的运维中心</p>
      </div>
      <div class="head-right">
        <div class="node-picker">
          <span class="node-label">节点</span>
          <UISelect v-model="nodeId" :options="nodeOptions" class="node-select" :disabled="!nodes.length" />
          <StatusDot :status="curNode ? (curNode.online ? 'running' : 'exited') : 'unknown'" />
        </div>
      </div>
    </div>

    <div v-if="!nodes.length" class="no-node-tip">
      还没有接入被控节点，请先到「节点管理」添加节点。
    </div>

    <UITabs v-model="tab" :tabs="tabs" class="tabs" />

    <!-- 容器管理 -->
    <template v-if="tab === 'containers'">
      <div class="sec-head">
        <span class="text-dim">共 {{ containers.length }} 个容器，{{ runningCount }} 个运行中（含非面板创建的容器）</span>
        <UIButton type="ghost" @click="loadContainers">刷新</UIButton>
      </div>
      <UITable :columns="contCols" :rows="containers">
        <template #col-name="{ row }">
          <span class="mono">{{ row.name }}</span>
          <UITag v-if="row.is_panel" tone="primary" class="ml6">面板实例</UITag>
        </template>
        <template #col-image="{ row }"><span class="mono text-dim">{{ row.image }}</span></template>
        <template #col-state="{ row }"><StatusDot :status="row.state" /></template>
        <template #col-ports="{ row }"><span class="mono">{{ row.ports }}</span></template>
        <template #col-created="{ row }">{{ fmtWhen(row.created) }}</template>
        <template #col-ops="{ row }">
          <UIButton v-if="row.state !== 'running'" type="text" @click="contAction(row, 'start')">启动</UIButton>
          <template v-else>
            <UIButton type="text" @click="contAction(row, 'stop')">停止</UIButton>
            <UIButton type="text" @click="contAction(row, 'restart')">重启</UIButton>
          </template>
          <UIButton type="text" @click="showLogs(row)">日志</UIButton>
          <UIButton type="text" class="danger" @click="onRemoveClick(row)">删除</UIButton>
        </template>
      </UITable>
    </template>

    <!-- 镜像管理 -->
    <template v-else-if="tab === 'images'">
      <div class="sec-head">
        <span class="text-dim">共 {{ images.length }} 个镜像</span>
        <UIButton type="ghost" @click="pruneImages">清理悬空镜像</UIButton>
      </div>
      <div class="pull-bar">
        <UIInput v-model="pullImageName" class="pull-input mono" placeholder="镜像名，如 python:3.13-slim 或 redis:7" @keyup.enter="startPull" />
        <UIButton :loading="pulling" :disabled="!pullImageName.trim()" @click="startPull">拉取镜像</UIButton>
      </div>
      <UITable :columns="imgCols" :rows="images">
        <template #col-full_name="{ row }"><span class="mono">{{ row.full_name }}</span></template>
        <template #col-created="{ row }">{{ fmtWhen(row.created) }}</template>
        <template #col-ops="{ row }">
          <UIButton type="text" class="danger" @click="removeImage(row)">删除</UIButton>
        </template>
      </UITable>

      <UIModal v-model:open="pullOpen" :title="`拉取镜像 - ${pullTitle}`" width="720px" persistent>
        <Terminal ref="pullTerm" placeholder="等待拉取..." class="pull-term-box" />
      </UIModal>
    </template>

    <!-- Docker 设置 -->
    <template v-else-if="tab === 'settings'">
      <div v-if="info" class="info-grid">
        <div v-for="[k, v] in infoItems" :key="k" class="info-cell">
          <span class="info-k">{{ k }}</span>
          <span class="info-v mono">{{ v }}</span>
        </div>
      </div>

      <div class="settings-card">
        <h3 class="card-title">镜像加速（registry-mirrors）</h3>
        <p class="card-tip">
          写入 <code class="mono">{{ info?.daemon_json_path || '/etc/docker/daemon.json' }}</code>，
          保存后自动重启 Docker 生效。常用加速地址：
          <span class="mono mirror-hint">https://docker.m.daocloud.io</span>、
          <span class="mono mirror-hint">https://dockerproxy.com</span>
        </p>
        <div class="mirror-list">
          <div v-for="(m, i) in mirrors" :key="i" class="mirror-row">
            <UIInput v-model="m.v" class="mono" placeholder="https://xxx.mirror.aliyuncs.com" />
            <UIButton type="ghost" class="danger" @click="rmMirror(i)">移除</UIButton>
          </div>
          <UIButton type="ghost" @click="addMirror">+ 添加加速地址</UIButton>
        </div>
        <div class="save-row">
          <UIButton :loading="savingSettings" @click="saveSettings">保存并重启 Docker</UIButton>
        </div>
      </div>
    </template>

    <!-- 宿主机终端 -->
    <template v-else>
      <HostTerminal :node-id="nodeId" />
    </template>

    <ConfirmDialog
      v-model:open="confirmOpen"
      :title="confirmCtx.title"
      :message="confirmCtx.message"
      confirm-text="确定"
      danger
      @confirm="confirmCtx.run"
    />

    <!-- 容器日志弹窗 -->
    <UIModal v-model:open="logsOpen" :title="`容器日志 - ${logsName}`" width="760px">
      <div class="log-pre mono">{{ logsText }}</div>
    </UIModal>
  </div>
</template>

<style scoped>
.tabs { margin-bottom: 18px; }
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.ml6 { margin-left: 6px; }

.head-right { display: flex; align-items: center; gap: 10px; }
.node-picker { display: flex; align-items: center; gap: 8px; }
.node-label { font-size: 13px; color: var(--text-dim); }
.node-select { width: 190px; }
.no-node-tip {
  background: var(--card-bg); border: 1px dashed var(--line); border-radius: var(--radius);
  padding: 26px; text-align: center; color: var(--text-dim); font-size: 13px; margin-bottom: 16px;
}

.pull-bar { display: flex; gap: 10px; margin-bottom: 14px; }
.pull-input { max-width: 380px; }
.pull-term-box { height: 340px; }

.info-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px;
}
.info-cell {
  background: var(--card-bg); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 12px 14px; display: flex; flex-direction: column; gap: 4px;
}
.info-k { font-size: 12px; color: var(--text-dim); }
.info-v { font-size: 14px; font-weight: 600; color: var(--text-1); }

.settings-card {
  background: var(--card-bg); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 18px 20px;
}
.card-title { font-size: 14px; font-weight: 700; margin: 0 0 6px; }
.card-tip { font-size: 12px; color: var(--text-dim); margin: 0 0 14px; line-height: 1.7; }
.mirror-hint { opacity: .8; }
.mirror-list { display: flex; flex-direction: column; gap: 10px; max-width: 560px; }
.mirror-row { display: flex; gap: 10px; }
.save-row { margin-top: 16px; }

.term-shell {
  background: #0c1613; border-radius: var(--radius); overflow: hidden;
  border: 1px solid #14231f; display: flex; flex-direction: column; height: 480px;
}
.term-box { flex: 1; border-radius: 0; background: transparent; }
.term-input-row {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  border-top: 1px solid #14231f; background: #0a1210;
}
.term-prompt { color: var(--primary); font-size: 12.5px; flex-shrink: 0; }
.term-input {
  flex: 1; background: transparent; border: none; outline: none;
  color: #d7efe7; font-size: 12.5px; font-family: inherit;
}
.term-input::placeholder { color: #3c544d; }

.log-pre {
  max-height: 55vh; overflow: auto; background: #0c1613; color: #a8d5c8;
  padding: 12px 14px; border-radius: 8px; font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
}
@media (max-width: 720px) { .info-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
