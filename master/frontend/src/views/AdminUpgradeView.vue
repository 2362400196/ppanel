<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITag from '../components/ui/UITag.vue'
import StatusDot from '../components/ui/StatusDot.vue'

/* ---------- 主控升级 ---------- */
const master = ref(null)          // /admin/system/version
const masterChecking = ref(false)
const masterUpgrading = ref(false)
const masterState = reactive({ stage: '', log: [], ok: null, error: '' })
let masterTimer = null

async function checkMaster() {
  masterChecking.value = true
  try {
    const { data } = await api.get('/admin/system/version')
    master.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    masterChecking.value = false
  }
}

async function upgradeMaster() {
  if (masterUpgrading.value) return
  try {
    await api.post('/admin/system/upgrade')
    masterUpgrading.value = true
    masterState.stage = '启动升级'
    masterState.log = []
    masterState.ok = null
    masterState.error = ''
    clearInterval(masterTimer)
    masterTimer = setInterval(pollMaster, 2000)
    pollMaster()
  } catch (e) {
    toastErr(errText(e))
  }
}

async function pollMaster() {
  try {
    const { data } = await api.get('/admin/system/upgrade/status')
    masterState.stage = data.stage
    masterState.log = data.log || []
    if (!data.running) {
      clearInterval(masterTimer)
      masterUpgrading.value = false
      if (data.ok) {
        masterState.ok = true
        toastOk('主控升级完成，服务已重启')
        setTimeout(checkMaster, 2500)   // 重启就绪后刷新版本
      } else {
        masterState.ok = false
        masterState.error = data.error
      }
    }
  } catch {
    /* 重启瞬间连接中断属预期，继续轮询 */
  }
}

/* ---------- 节点升级 ---------- */
const nodes = ref([])
const nodeVer = reactive({})     // id -> version 信息
const nodeUp = reactive({})      // id -> { running, stage, log, ok, error }
const nodeTimers = {}

async function loadNodes() {
  try {
    const { data } = await api.get('/admin/nodes')
    nodes.value = data
  } catch (e) {
    toastErr(errText(e))
  }
}

async function checkNode(n) {
  try {
    const { data } = await api.get(`/admin/nodes/${n.id}/version`)
    nodeVer[n.id] = data
  } catch (e) {
    nodeVer[n.id] = { mode: 'error', reason: errText(e) }
  }
}

async function upgradeNode(n) {
  if (nodeUp[n.id]?.running) return
  try {
    await api.post(`/admin/nodes/${n.id}/upgrade`)
    nodeUp[n.id] = { running: true, stage: '启动升级', log: [], ok: null, error: '' }
    clearInterval(nodeTimers[n.id])
    nodeTimers[n.id] = setInterval(() => pollNode(n), 2000)
  } catch (e) {
    toastErr(errText(e))
  }
}

async function pollNode(n) {
  try {
    const { data } = await api.get(`/admin/nodes/${n.id}/upgrade/status`)
    nodeUp[n.id] = { ...nodeUp[n.id], ...data }
    if (!data.running) {
      clearInterval(nodeTimers[n.id])
      if (data.ok) {
        toastOk(`节点「${n.name}」升级完成，正在重启`)
        setTimeout(() => { checkNode(n); loadNodes() }, 5000)
      }
    }
  } catch {
    /* agent 重启瞬间连接中断属预期；连续失败由用户手动「检查更新」确认 */
  }
}

let timer
onMounted(() => {
  checkMaster()
  loadNodes()
  timer = setInterval(loadNodes, 15000)
})
onUnmounted(() => {
  clearInterval(timer)
  clearInterval(masterTimer)
  Object.values(nodeTimers).forEach(clearInterval)
})
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">系统升级</h1>
        <p class="page-sub">主控与被控在线升级：拉取仓库代码 → 安装依赖 → 构建 → 自动重启，数据与配置不受影响</p>
      </div>
    </div>

    <!-- 主控升级 -->
    <section class="sec">
      <div class="sec-head">
        <h2 class="sec-title">主控面板</h2>
        <UIButton type="ghost" size="sm" :disabled="masterChecking || masterUpgrading" @click="checkMaster">
          {{ masterChecking ? '检查中...' : '检查更新' }}
        </UIButton>
      </div>
      <div v-if="master" class="ver-row">
        <div class="ver-item">
          <span class="k">当前版本</span>
          <span class="v mono">{{ master.current || '—' }}</span>
        </div>
        <div class="ver-item">
          <span class="k">远程版本</span>
          <span class="v mono">{{ master.remote || '—' }}</span>
        </div>
        <div class="ver-item">
          <span class="k">分支</span>
          <span class="v mono">{{ master.branch || '—' }}</span>
        </div>
        <div class="ver-item">
          <span class="k">状态</span>
          <template v-if="master.mode !== 'git'">
            <UITag tone="dim">{{ master.reason || '不可在线升级' }}</UITag>
          </template>
          <UITag v-else-if="master.upgradable" tone="warn">可升级</UITag>
          <UITag v-else-if="master.remote" tone="ok">已是最新</UITag>
          <UITag v-else tone="dim">远程检查失败（网络）</UITag>
        </div>
        <UIButton v-if="master.mode === 'git' && master.upgradable" :disabled="masterUpgrading" @click="upgradeMaster">
          {{ masterUpgrading ? '升级中...' : '一键升级' }}
        </UIButton>
      </div>
      <div v-else class="dim">加载中...</div>

      <div v-if="masterUpgrading || masterState.ok !== null" class="prog" :class="{ fail: masterState.ok === false }">
        <div class="prog-stage">
          <span v-if="masterUpgrading" class="spin"></span>
          <span v-else>{{ masterState.ok ? '✔' : '✘' }}</span>
          {{ masterState.stage }}
          <span v-if="masterState.stage === '重启服务' && masterUpgrading" class="dim">（服务重启中，几秒后自动恢复）</span>
        </div>
        <pre v-if="masterState.log.length" class="prog-log">{{ masterState.log.join('\n') }}</pre>
        <div v-if="masterState.error" class="prog-err">{{ masterState.error }}</div>
      </div>
    </section>

    <!-- 节点升级 -->
    <section class="sec">
      <div class="sec-head">
        <h2 class="sec-title">被控节点</h2>
        <UIButton type="ghost" size="sm" @click="loadNodes">刷新</UIButton>
      </div>
      <div v-if="!nodes.length" class="dim">暂无节点</div>
      <div v-for="n in nodes" :key="n.id" class="node-row">
        <div class="node-main">
          <StatusDot :status="n.online ? 'running' : 'exited'" />
          <div class="node-info">
            <span class="node-name">{{ n.name }}</span>
            <span class="node-url mono">{{ n.base_url }}</span>
          </div>
        </div>
        <div class="node-ver">
          <template v-if="nodeVer[n.id]">
            <template v-if="nodeVer[n.id].mode === 'git'">
              <span class="mono dim">{{ nodeVer[n.id].current || '—' }}</span>
              <span class="arrow" :class="{ up: nodeVer[n.id].upgradable }">→</span>
              <span class="mono" :class="nodeVer[n.id].upgradable ? 'warn-t' : 'dim'">{{ nodeVer[n.id].remote || '—' }}</span>
              <UITag v-if="nodeVer[n.id].upgradable" tone="warn">可升级</UITag>
              <UITag v-else tone="ok">最新</UITag>
            </template>
            <UITag v-else tone="dim">{{ nodeVer[n.id].reason || '不可升级' }}</UITag>
          </template>
          <template v-else>
            <span class="dim">未检查</span>
          </template>
        </div>
        <div class="node-ops">
          <UIButton type="ghost" size="sm" :disabled="!n.online || nodeUp[n.id]?.running" @click="checkNode(n)">检查更新</UIButton>
          <UIButton v-if="nodeVer[n.id]?.upgradable" size="sm" :disabled="nodeUp[n.id]?.running" @click="upgradeNode(n)">
            {{ nodeUp[n.id]?.running ? '升级中' : '升级' }}
          </UIButton>
        </div>
        <div v-if="nodeUp[n.id]" class="node-prog" :class="{ open: nodeUp[n.id].running || nodeUp[n.id].ok === false }">
          <div class="prog-stage">
            <span v-if="nodeUp[n.id].running" class="spin"></span>
            <span v-else>{{ nodeUp[n.id].ok ? '✔' : '✘' }}</span>
            {{ nodeUp[n.id].stage }}
          </div>
          <pre v-if="nodeUp[n.id].log?.length" class="prog-log">{{ nodeUp[n.id].log.join('\n') }}</pre>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.sec {
  border: 1px solid var(--line); border-radius: 14px; background: var(--bg-1);
  padding: 18px 20px; margin-bottom: 18px;
}
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.sec-title { font-size: 15px; font-weight: 700; }
.ver-row { display: flex; align-items: center; gap: 26px; flex-wrap: wrap; }
.ver-item { display: flex; flex-direction: column; gap: 3px; }
.k { font-size: 12px; color: var(--text-dim); }
.v { font-size: 15px; font-weight: 600; }
.mono { font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace; }
.dim { color: var(--text-dim); }
.warn-t { color: var(--warn, #d97706); font-weight: 700; }
.arrow { color: var(--text-dim); margin: 0 4px; }
.arrow.up { color: var(--warn, #d97706); }

.prog {
  margin-top: 14px; border-top: 1px dashed var(--line); padding-top: 12px;
  animation: fadeIn .25s ease;
}
.prog-stage { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 14px; }
.prog-log {
  margin: 10px 0 0; padding: 10px 12px; border-radius: 8px;
  background: #0c1512; color: #9fe8c8; font-size: 12px; line-height: 1.6;
  max-height: 220px; overflow: auto; white-space: pre-wrap;
}
.prog-err { margin-top: 8px; color: var(--danger, #e5484d); font-size: 13px; }
.prog.fail .prog-stage { color: var(--danger, #e5484d); }

.node-row {
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
  padding: 12px 4px; border-top: 1px solid var(--line);
}
.node-row:first-of-type { border-top: none; }
.node-main { display: flex; align-items: center; gap: 10px; min-width: 200px; }
.node-info { display: flex; flex-direction: column; }
.node-name { font-weight: 700; font-size: 14px; }
.node-url { font-size: 12px; color: var(--text-dim); }
.node-ver { display: flex; align-items: center; gap: 8px; flex: 1; }
.node-ops { display: flex; gap: 8px; }
.node-prog {
  width: 100%; display: none; border-top: 1px dashed var(--line); padding-top: 10px;
}
.node-prog.open { display: block; animation: fadeIn .25s ease; }

.spin {
  width: 14px; height: 14px; border-radius: 50%;
  border: 2px solid var(--line); border-top-color: var(--primary);
  animation: spin .8s linear infinite; display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; } }
</style>
