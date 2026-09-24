<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { wsUrl } from '../api/client'
import UIButton from './ui/UIButton.vue'
import UIInput from './ui/UIInput.vue'
import UIModal from './ui/UIModal.vue'

// 宿主机交互式终端（xterm.js PTY）：本机 shell 或 SSH 登录远程节点
const props = defineProps({
  nodeId: { type: [String, Number], default: '' }
})

const boxEl = ref(null)
const status = ref('idle') // idle | connecting | connected | closed
const mode = ref('local') // local | ssh | none
const label = ref('')
const sshOpen = ref(false)
const sshBusy = ref(false)
const sshForm = ref({ host: '', port: 22, user: 'root', password: '' })
let pendingSSH = null
let term = null
let fit = null
let ws = null
let ro = null
let ping = null

const statusText = computed(() => ({
  idle: '未连接', connecting: '连接中...', connected: '已连接', closed: '已断开'
}[status.value]))

async function ensureXterm() {
  if (window.Terminal && window.FitAddon) return
  if (!document.querySelector('link[data-xterm]')) {
    const l = document.createElement('link')
    l.rel = 'stylesheet'
    l.href = '/vendor/xterm/xterm.css'
    l.setAttribute('data-xterm', '1')
    document.head.appendChild(l)
  }
  const load = src => new Promise((ok, bad) => {
    const s = document.createElement('script')
    s.src = src
    s.onload = ok
    s.onerror = bad
    document.head.appendChild(s)
  })
  await load('/vendor/xterm/xterm.js')
  await load('/vendor/xterm/addon-fit.js')
}

function initTerm() {
  if (term) return
  term = new window.Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: 'Consolas, "Courier New", monospace',
    theme: {
      background: '#10161a',
      foreground: '#c7e5df',
      cursor: '#2fb59f',
      cursorAccent: '#10161a',
      selectionBackground: 'rgba(47,181,159,.3)',
      black: '#10161a', green: '#2fb59f', cyan: '#5fd7c5'
    }
  })
  fit = new window.FitAddon.FitAddon()
  term.loadAddon(fit)
  term.open(boxEl.value)
  term.onData(d => send({ type: 'input', data: d }))
  term.onResize(({ cols, rows }) => send({ type: 'resize', cols, rows }))
  ro = new ResizeObserver(() => {
    if (!boxEl.value.offsetParent) return
    try { fit.fit() } catch { /* 尺寸异常忽略 */ }
  })
  ro.observe(boxEl.value)
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj))
}

function size() {
  let cols = 120, rows = 30
  try { fit.fit() } catch { /* 忽略 */ }
  if (term) { cols = term.cols; rows = term.rows }
  return { cols, rows }
}

async function connect() {
  if (!props.nodeId) return
  await ensureXterm()
  initTerm()
  if (ws && ws.readyState <= WebSocket.OPEN) return
  status.value = 'connecting'
  const { cols, rows } = size()
  ws = new WebSocket(wsUrl('/ws/host/term', { node: props.nodeId }))
  ws.onopen = () => {
    status.value = 'connected'
    term.reset()
    send({ type: 'resize', cols, rows })
    if (pendingSSH) {
      send({ type: 'connect', ...pendingSSH, cols, rows })
      pendingSSH = null
    }
    term.focus()
    clearInterval(ping)
    ping = setInterval(() => send({ type: 'input', data: '' }), 30000)
  }
  ws.onmessage = ev => {
    let m
    try { m = JSON.parse(ev.data) } catch { return }
    if (m.type === 'data') term.write(m.data)
    else if (m.type === 'mode') { mode.value = m.mode; label.value = m.label || '' }
    else if (m.type === 'error') {
      term.write('\r\n\x1b[31m' + (m.msg || '终端不可用') + '\x1b[0m\r\n')
      status.value = 'closed'
    } else if (m.type === 'closed') {
      term.write('\r\n\x1b[33m[连接已断开]\x1b[0m\r\n')
      status.value = 'closed'
    }
  }
  ws.onclose = () => {
    if (ws) { ws = null }
    clearInterval(ping)
    if (status.value !== 'idle') status.value = 'closed'
  }
  ws.onerror = () => { if (status.value !== 'closed') status.value = 'closed' }
}

function stop() {
  if (ws) { try { ws.close() } catch { /* 忽略 */ } ws = null }
  clearInterval(ping)
  status.value = 'idle'
  term?.reset()
}

function reconnect() { stop(); connect() }
function clearTerm() { term?.clear() }

// ---------- SSH 登录 ----------
function openSSH() {
  sshForm.value = { host: '', port: 22, user: 'root', password: '' }
  sshOpen.value = true
}

function doSSH() {
  const f = sshForm.value
  if (!f.host.trim()) return
  pendingSSH = { host: f.host.trim(), port: Number(f.port) || 22, user: f.user.trim() || 'root', password: f.password }
  sshOpen.value = false
  mode.value = 'ssh'
  label.value = `${pendingSSH.user}@${pendingSSH.host}:${pendingSSH.port}（连接中）`
  stop()
  connect()
}

function backLocal() {
  pendingSSH = null
  mode.value = 'local'
  stop()
  connect()
}

watch(() => props.nodeId, () => { pendingSSH = null; mode.value = 'local'; stop(); connect() })
onMounted(connect)
onUnmounted(() => {
  stop()
  ro?.disconnect()
  try { term?.dispose() } catch { /* 忽略 */ }
  term = null
})
</script>

<template>
  <div class="ht-wrap">
    <div class="ht-head">
      <span class="ht-status" :class="status"><i class="dot" />{{ statusText }}</span>
      <span class="ht-label mono">{{ mode === 'ssh' ? 'SSH ' + label : label }}</span>
      <span class="ht-tip">直接敲命令；SSH 登录后可跳转到任意节点</span>
      <div class="ht-ops">
        <button v-if="mode === 'ssh'" class="ht-btn" @click="backLocal">返回本机</button>
        <button v-else class="ht-btn" @click="openSSH">SSH 登录</button>
        <button class="ht-btn" @click="reconnect">重连</button>
        <button class="ht-btn" @click="clearTerm">清屏</button>
      </div>
    </div>
    <div ref="boxEl" class="ht-box" @click="term?.focus()" />

    <UIModal v-model:open="sshOpen" title="SSH 登录远程主机" width="420px">
      <div class="ssh-form">
        <label class="field">
          <span>主机</span>
          <UIInput v-model="sshForm.host" class="mono" placeholder="如 192.168.31.149" @keyup.enter="doSSH" />
        </label>
        <label class="field">
          <span>端口</span>
          <UIInput v-model="sshForm.port" type="number" />
        </label>
        <label class="field">
          <span>用户名</span>
          <UIInput v-model="sshForm.user" class="mono" placeholder="root" />
        </label>
        <label class="field">
          <span>密码</span>
          <UIInput v-model="sshForm.password" type="password" placeholder="root 密码" @keyup.enter="doSSH" />
        </label>
        <p class="ssh-tip">密码经本节点中转建立 SSH 会话，不落库、不记录日志。</p>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="sshOpen = false">取消</UIButton>
        <UIButton :disabled="!sshForm.host.trim()" @click="doSSH">连接</UIButton>
      </template>
    </UIModal>
  </div>
</template>

<style scoped>
.ht-wrap { border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
.ht-head {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 14px; background: var(--card-bg); border-bottom: 1px solid var(--line);
}
.ht-status { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; white-space: nowrap; }
.ht-status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-dim); }
.ht-status.connecting .dot { background: #f59e0b; animation: htPulse 1.2s ease-in-out infinite; }
.ht-status.connected .dot { background: #10b981; animation: htPulse 2s ease-in-out infinite; }
.ht-status.closed .dot { background: #9aa5a1; }
@keyframes htPulse { 50% { opacity: .35; } }
.ht-label { font-size: 12px; color: var(--primary); white-space: nowrap; }
.ht-tip { flex: 1; font-size: 12px; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ht-ops { display: flex; gap: 8px; }
.ht-btn {
  height: 26px; padding: 0 12px; font-size: 12px; font-family: inherit;
  border: 1px solid var(--line); border-radius: 6px; background: transparent;
  color: var(--text-2); cursor: pointer; transition: all .15s; white-space: nowrap;
}
.ht-btn:hover { border-color: var(--primary); color: var(--primary); }
.ht-box {
  height: 62vh; padding: 8px 10px; background: #10161a; overflow: hidden;
}
.ht-box :deep(.xterm) { height: 100%; }
.ht-box :deep(.xterm-viewport) { background: transparent !important; }
.ht-box :deep(.xterm-viewport::-webkit-scrollbar) { width: 6px; }
.ht-box :deep(.xterm-viewport::-webkit-scrollbar-thumb) { background: #2a2a30; border-radius: 3px; }

.ssh-form { display: flex; flex-direction: column; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field span { font-size: 12.5px; color: var(--text-2); }
.ssh-tip { margin: 0; font-size: 12px; color: var(--text-dim); }
</style>
