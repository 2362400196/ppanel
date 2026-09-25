<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from './ui/UIButton.vue'
import UIInput from './ui/UIInput.vue'
import UIModal from './ui/UIModal.vue'
import UISelect from './ui/UISelect.vue'
import UIPager from './ui/UIPager.vue'
import UITable from './ui/UITable.vue'
import UITag from './ui/UITag.vue'
import StatusDot from './ui/StatusDot.vue'
import ConfirmDialog from './ui/ConfirmDialog.vue'
import CodeEditor from './CodeEditor.vue'
import HostTerminal from './HostTerminal.vue'
import { startTask } from '../api/tasks'

// 节点管理面板：一个大模态框聚合节点级运维能力（概览/容器/镜像/文件/终端）
const props = defineProps({
  open: { type: Boolean, default: false },
  node: { type: Object, default: null } // { id, name }
})
const emit = defineEmits(['update:open'])

const view = ref('overview')
const np = () => ({ node: String(props.node?.id || '') })

// ---------- 概览（资源仪表盘） ----------
const info = ref(null)
const stats = ref(null)
const RING_C = 2 * Math.PI * 52

// ---------- 稳定性评分 ----------
const stab = ref(null)
const stabWin = ref(7)
const stabTone = g => ({ excellent: 'ok', good: 'ok', fair: 'warn', poor: 'warn', observing: 'dim' }[g] || 'dim')
async function loadStability() {
  if (!props.node?.id) return
  try { stab.value = (await api.get(`/admin/nodes/${props.node.id}/stability`, { params: { window: stabWin.value } })).data }
  catch (e) { stab.value = null }
}

function fmtUptime(sec) {
  if (!sec) return '-'
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60)
  return d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${m} 分钟`
}

const gauges = computed(() => {
  const s = stats.value
  if (!s) return []
  const color = p => p == null ? 'var(--line)' : p >= 85 ? '#e5484d' : p >= 60 ? '#f59e0b' : 'var(--primary)'
  // 容器运行率语义与资源占用相反：越高越健康 → 高绿低红
  const upColor = p => p == null ? 'var(--line)' : p >= 60 ? 'var(--primary)' : p >= 30 ? '#f59e0b' : '#e5484d'
  const c = s.cpu || {}, m = s.mem || {}, d = s.disk || {}, ct = s.containers
  const contP = ct && ct.total ? Math.round((ct.running || 0) / ct.total * 100) : null
  return [
    { label: 'CPU', p: c.percent, color: color(c.percent), sub: c.cores ? `${c.cores} 核` : '—' },
    { label: '内存', p: m.percent, color: color(m.percent), sub: m.total ? `${fmtSize(m.used)} / ${fmtSize(m.total)}` : '—' },
    { label: '磁盘', p: d.percent, color: color(d.percent), sub: d.total ? `${fmtSize(d.used)} / ${fmtSize(d.total)}` : '—' },
    { label: '容器运行', p: contP, color: upColor(contP), sub: ct ? `${ct.running} 运行 / ${ct.paused} 暂停 / ${ct.stopped} 停止` : '—' }
  ]
})

const infoItems = computed(() => {
  if (!info.value) return []
  const s = stats.value || {}
  return [
    ['操作系统', info.value.os || '-'],
    ['CPU 核数', s.cpu?.cores || info.value.cpus || '-'],
    ['内存总量', s.mem?.total ? fmtSize(s.mem.total) : (info.value.mem_total_gb ? `${info.value.mem_total_gb} GB` : '-')],
    ['系统负载', s.load ? s.load.join(' / ') : '-'],
    ['运行时长', fmtUptime(s.uptime)],
    ['Docker 版本', info.value.server_version || '-'],
    ['存储驱动', info.value.storage_driver || '-'],
    ['内核', info.value.kernel || '-'],
    ['架构', info.value.arch || '-'],
  ]
})

async function loadStats() {
  try { stats.value = (await api.get('/host/stats', { params: np() })).data }
  catch (e) { toastErr(errText(e)) }
}

// 面板打开且停留在概览时，5s 轮询资源数据
let statTimer = null
function syncPoll() {
  const on = props.open && view.value === 'overview'
  if (on && !statTimer) {
    loadStats()
    loadInfo()
    statTimer = setInterval(() => { loadStats(); loadInfo() }, 5000)
  } else if (!on && statTimer) {
    clearInterval(statTimer)
    statTimer = null
  }
}

// ---------- 容器 / 镜像 ----------
const containers = ref([])
const images = ref([])

async function loadInfo() {
  try { info.value = (await api.get('/docker/info', { params: np() })).data }
  catch (e) { toastErr(errText(e)) }
}
async function loadContainers() {
  try { containers.value = (await api.get('/docker/containers', { params: np() })).data }
  catch (e) { toastErr(errText(e)) }
}
async function loadImages() {
  try { images.value = (await api.get('/docker/images', { params: np() })).data }
  catch (e) { toastErr(errText(e)) }
}
async function contAction(c, action) {
  try {
    await api.post(`/docker/containers/${c.id}/${action}`, null, { params: np() })
    toastOk(`容器已${action === 'start' ? '启动' : action === 'stop' ? '停止' : '重启'}`)
    loadContainers()
  } catch (e) { toastErr(errText(e)) }
}

// 容器删除（运行中则强制）
const contDelOpen = ref(false)
const contDelTarget = ref(null)
function askDelContainer(c) {
  contDelTarget.value = c
  contDelOpen.value = true
}
async function confirmDelContainer() {
  const c = contDelTarget.value
  contDelOpen.value = false
  if (!c) return
  try {
    // running/restarting/paused 必须强制删除（docker rm 对这些状态非 -f 会失败）
    const { data } = await api.delete(`/docker/containers/${c.id}`, {
      params: { ...np(), force: ['running', 'restarting', 'paused'].includes(c.state) ? 1 : '' }
    })
    toastOk(data.detail || '已删除')
    loadContainers()
  } catch (e) { toastErr(errText(e)) }
}

// 容器日志
const logOpen = ref(false)
const logName = ref('')
const logText = ref('')
async function showLogs(c) {
  logName.value = c.name
  logText.value = '加载中…'
  logOpen.value = true
  try {
    const { data } = await api.get(`/docker/containers/${c.id}/logs`, { params: { ...np(), tail: 500 } })
    logText.value = data.logs || '(无日志)'
  } catch (e) { logOpen.value = false; toastErr(errText(e)) }
}

// 镜像删除（被占用时二次确认强制删除）
const imgDelOpen = ref(false)
const imgDelTarget = ref(null)
const imgDelForce = ref(false)
function askDelImage(img) {
  imgDelTarget.value = img
  imgDelForce.value = false
  imgDelOpen.value = true
}
async function confirmDelImage() {
  const img = imgDelTarget.value
  imgDelOpen.value = false
  if (!img) return
  try {
    const { data } = await api.delete(`/docker/images/${img.full_name}`, { params: { ...np(), force: imgDelForce.value } })
    toastOk(data.detail || '已删除')
    loadImages()
  } catch (e) {
    const msg = errText(e)
    if (String(e?.response?.status) === '409' && !imgDelForce.value) {
      // 被容器占用 → 二次确认强制删除
      imgDelForce.value = true
      imgDelOpen.value = true
      return
    }
    toastErr(msg)
  }
}

// 拉取镜像
const pullOpen = ref(false)
const pullImage = ref('')
const pullLoading = ref(false)
async function doPull() {
  const image = pullImage.value.trim()
  if (!image) { toastErr('请输入镜像名'); return }
  pullLoading.value = true
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post('/docker/images/pull', { image },
      { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
    toastOk(`拉取完成：${data.image?.full_name || image}（${data.image?.size || ''}）`)
    pullOpen.value = false
    pullImage.value = ''
    loadImages()
    loadInfo()
  } catch (e) { toastErr(errText(e)) }
  finally { pullLoading.value = false }
}

// 清理悬空镜像
const pruning = ref(false)
async function pruneImages() {
  pruning.value = true
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post('/docker/images/prune', null,
      { params: np(), headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '清理完成')
    loadImages()
    loadInfo()
  } catch (e) { toastErr(errText(e)) }
  finally { pruning.value = false }
}

// 构建 PHP 增强镜像：官方 cli 基底 + 编译 mysqli/pdo_mysql/gd/zip/bcmath/intl/sockets/exif/opcache
// 产物 ppanel-php:8.x-full，被控动态发现后自动加入运行环境白名单
const phpBuildOpen = ref(false)
const phpBuildBase = ref('')
const phpBuildLines = ref([])
const phpBuildDone = ref(false)
const phpBuildBusy = ref(false)
let phpBuildTimer = null
const phpBuildBaseOptions = computed(() =>
  images.value.filter(i => /^php:\d+\.\d+-cli$/.test(i.full_name)).map(i => i.full_name))

function askPhpBuild() {
  phpBuildLines.value = []
  phpBuildDone.value = false
  phpBuildBase.value = phpBuildBaseOptions.value[0] || ''
  phpBuildOpen.value = true
}

async function doPhpBuild() {
  if (!phpBuildBase.value) { toastErr('请先拉取 php 官方镜像（如 php:8.3-cli）再构建'); return }
  phpBuildBusy.value = true
  try {
    const { data } = await api.post('/docker/php-images/build', { base: phpBuildBase.value },
      { params: np(), timeout: 60000 })
    phpBuildLines.value = [`[面板] 已提交构建：${data.image}`]
    const poll = async () => {
      try {
        const { data: st } = await api.get(`/docker/php-images/build/${data.job_id}`, { params: np(), timeout: 20000 })
        phpBuildLines.value = st.lines || []
        if (st.done) {
          phpBuildDone.value = true
          clearInterval(phpBuildTimer); phpBuildTimer = null
          if (st.exit_code === 0) { toastOk(`构建完成：${st.image}`); loadImages(); loadInfo() }
          else toastErr('构建失败，请查看日志排查')
        }
      } catch (e) { /* 单次轮询失败忽略，下一轮重试 */ }
    }
    await poll()
    phpBuildTimer = setInterval(poll, 1200)
  } catch (e) { toastErr(errText(e)) }
  finally { phpBuildBusy.value = false }
}

watch(phpBuildOpen, v => {
  if (!v && phpBuildTimer) { clearInterval(phpBuildTimer); phpBuildTimer = null }
})

// ============================================================
// 文件管理（与独立面板 /panel 同款：iconfont 图标 + 多选 + 右键菜单
// + 剪贴板 + 压缩解压 + 图片预览 + Monaco 编辑 + 分片上传断点续传）
// ============================================================
const curPath = ref('/')
const entries = ref([])
const fLoading = ref(false)
const selection = reactive(new Set())   // Windows 风格多选
const dirSizes = reactive({})           // 目录大小缓存
const clip = ref(null)                  // { mode:'copy'|'cut', items:[], from }
const anchorName = ref('')

const IMG_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.ico', '.svg']
const ZIP_EXTS = ['.zip', '.rar', '.7z', '.tar', '.gz', '.tgz']

// iconfont（阿里 SVG symbol，与独立面板一致）
const iconTick = ref(0)
function ensureIconfont() {
  if (window.__ppIconfont) { iconTick.value++; return }
  const s = document.createElement('script')
  s.src = '/iconfont.js'
  s.onload = () => { window.__ppIconfont = true; iconTick.value++ }
  document.head.appendChild(s)
}
function fileIconId(name, isDir) {
  if (isDir) return 'icon-wenjianleixing-biaozhuntu-wenjianjia'
  const n = (name || '').toLowerCase()
  if (ZIP_EXTS.some(e => n.endsWith(e))) return 'icon-yasuobao'
  if (n.endsWith('.py')) return 'icon-py'
  if (n.endsWith('.html') || n.endsWith('.htm')) return 'icon-HTML'
  if (n.endsWith('.css')) return 'icon-CSS'
  if (n.endsWith('.js') || n.endsWith('.mjs')) return 'icon-javascript'
  if (n.endsWith('.mp4') || n.endsWith('.mkv') || n.endsWith('.avi')) return 'icon-shipin'
  if (n.endsWith('.php')) return 'icon-icon-PHP'
  if (IMG_EXTS.some(e => n.endsWith(e))) return 'icon-tupian'
  return 'icon-wenjian'
}
function isImage(name) { const n = (name || '').toLowerCase(); return IMG_EXTS.some(e => n.endsWith(e)) }
function isZip(name) { const n = (name || '').toLowerCase(); return ZIP_EXTS.some(e => n.endsWith(e)) }
function editable(name) { return !isImage(name) && !isZip(name) }

// 菜单/按钮内联线条图标（与独立面板同款 path）
const BI = {
  folderOpen: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1H3z"/><path d="M3 10h19l-2 8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  edit: '<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  eye: '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>',
  unzip: '<polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/>',
  zip: '<line x1="16.5" y1="9.4" x2="7.5" y2="4.21"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  cut: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/>',
  folderPlus: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/>',
  filePlus: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="12" x2="12" y2="18"/><line x1="9" y1="15" x2="15" y2="15"/>',
  clipboard: '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.5 15a9 9 0 1 1-2-9.5L23 10"/>',
  check: '<polyline points="20 6 9 17 4 12"/>'
}
function biSvg(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${BI[name] || ''}</svg>`
}

function fmtSize(n) {
  if (n === '' || n == null) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`
  return `${(n / 1073741824).toFixed(2)} GB`
}
function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const p = x => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function fmtIso(iso) {
  if (!iso) return '-'
  return fmtTime(new Date(iso).getTime() / 1000)
}
function joinPath(dir, name) { return dir === '/' ? `/${name}` : `${dir}/${name}` }
function parentOf(p) {
  if (!p || p === '/') return '/'
  const i = p.lastIndexOf('/')
  return i <= 0 ? '/' : p.slice(0, i)
}
const crumbs = computed(() => {
  const parts = (curPath.value || '/').split('/').filter(Boolean)
  const list = [{ name: '/', path: '/' }]
  let acc = ''
  for (const p of parts) { acc += '/' + p; list.push({ name: p, path: acc }) }
  return list
})

async function loadFiles(path) {
  fLoading.value = true
  try {
    const { data } = await api.get('/host/files', { params: { ...np(), path } })
    curPath.value = data.path
    entries.value = data.entries
    selection.clear()
    anchorName.value = ''
  } catch (e) { toastErr(errText(e)) }
  finally { fLoading.value = false }
}
function go(p) { loadFiles(p) }

// ---------- 选择（仅点击勾选框选中；行点击不选中，双击打开，右键操作当前行） ----------
function toggleRow(name) {
  selection.has(name) ? selection.delete(name) : selection.add(name)
  anchorName.value = name
}
function toggleAll() {
  if (entries.value.length && selection.size === entries.value.length) selection.clear()
  else entries.value.forEach(f => selection.add(f.name))
}
function clearSel() { selection.clear(); anchorName.value = '' }

// ---------- 右键菜单（防溢出定位 + 外部点击/ESC/滚动关闭） ----------
const ctxOpen = ref(false)
const ctxX = ref(0)
const ctxY = ref(0)
const ctxItems = ref([])
function openCtx(x, y, items) {
  ctxItems.value = items
  ctxX.value = x
  ctxY.value = y
  ctxOpen.value = true
  nextTick(() => {
    const m = document.querySelector('.nctx-menu')
    if (!m) return
    const r = m.getBoundingClientRect()
    if (x + r.width > innerWidth - 8) ctxX.value = Math.max(8, innerWidth - r.width - 8)
    if (y + r.height > innerHeight - 8) ctxY.value = Math.max(8, innerHeight - r.height - 8)
  })
}
function closeCtx() { ctxOpen.value = false }
function rowMenu(ev, it) {
  ev.preventDefault()
  ev.stopPropagation()
  // 右键未选中的行：只对当前行操作，不改勾选状态；已勾选则按整批操作
  const names = selection.has(it.name) ? [...selection] : [it.name]
  const multi = names.length > 1
  const items = []
  if (it.is_dir) {
    if (!multi) items.push({ label: '打开', icon: 'folderOpen', fn: () => go(joinPath(curPath.value, it.name)) })
    items.push({ label: multi ? `压缩 ${names.length} 项` : '压缩', icon: 'zip', fn: () => openZipSel(names) })
  } else {
    if (!multi) {
      if (editable(it.name)) items.push({ label: '编辑', icon: 'edit', fn: () => openFile(joinPath(curPath.value, it.name)) })
      items.push({ label: '下载', icon: 'download', fn: () => downloadFile(it.name) })
      if (isImage(it.name)) items.push({ label: '预览', icon: 'eye', fn: () => previewImg(it.name) })
      if (isZip(it.name)) items.push({ label: '解压', icon: 'unzip', fn: () => openUnzip(it.name) })
    }
    items.push({ label: multi ? `压缩 ${names.length} 项` : '压缩', icon: 'zip', fn: () => openZipSel(names) })
  }
  items.push({ sep: true })
  items.push({ label: '复制', icon: 'copy', fn: () => clipCopy(names) })
  items.push({ label: '剪切', icon: 'cut', fn: () => clipCut(names) })
  items.push({ sep: true })
  if (!multi) items.push({ label: '重命名', icon: 'edit', fn: () => promptRename(it.name) })
  items.push({ label: multi ? `删除 ${names.length} 项` : '删除', icon: 'trash', danger: true, fn: () => delItems(names) })
  openCtx(ev.clientX, ev.clientY, items)
}
function blankMenu(ev) {
  if (ev.target.closest('tr, button, .ck, .sel-bar, .nctx-menu, select, input, a')) return
  ev.preventDefault()
  openCtx(ev.clientX, ev.clientY, [
    { label: '新建文件夹', icon: 'folderPlus', fn: promptMkdir },
    { label: '新建文件', icon: 'filePlus', fn: promptNewFile },
    { sep: true },
    { label: '粘贴', icon: 'clipboard', disabled: !clip.value, fn: doPaste },
    { sep: true },
    { label: '刷新', icon: 'refresh', fn: () => loadFiles(curPath.value) }
  ])
}
function onDocMouseDown(ev) {
  if (ctxOpen.value && !ev.target.closest('.nctx-menu')) closeCtx()
}
function onGlobalKey(ev) {
  if (!props.open || view.value !== 'files') return
  if (ev.target.closest('.monaco-editor, input, textarea, select')) return
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'a') {
    ev.preventDefault()
    entries.value.forEach(f => selection.add(f.name))
  } else if (ev.key === 'Escape') {
    clearSel()
    closeCtx()
  }
}

// ---------- 剪贴板 ----------
function clipCopy(names) {
  const list = names && names.length ? names : [...selection]
  if (!list.length) return
  clip.value = { mode: 'copy', items: list, from: curPath.value }
  toastOk(`已复制 ${list.length} 项，请到目标目录粘贴`)
}
function clipCut(names) {
  const list = names && names.length ? names : [...selection]
  if (!list.length) return
  clip.value = { mode: 'cut', items: list, from: curPath.value }
  toastOk(`已剪切 ${list.length} 项，请到目标目录粘贴`)
}
async function doPaste() {
  if (!clip.value) return
  if (clip.value.mode === 'cut' && clip.value.from === curPath.value) {
    toastErr('剪切的原位置与目标位置相同')
    return
  }
  for (const name of clip.value.items) {
    const src = joinPath(clip.value.from, name), dst = joinPath(curPath.value, name)
    try {
      if (clip.value.mode === 'copy') await api.post('/host/files/copy', { src, dst }, { params: np() })
      else await api.post('/host/files/rename', { src, dst }, { params: np() })
    } catch (e) { toastErr(`粘贴 ${name} 失败：${errText(e)}`); return }
  }
  toastOk(`粘贴完成（${clip.value.items.length} 项）`)
  clip.value = null
  loadFiles(curPath.value)
}

// ---------- 输入弹窗（新建/重命名） ----------
const promptOpen = ref(false)
const promptTitle = ref('')
const promptLabel = ref('')
const promptValue = ref('')
const promptFn = ref(null)
function promptInput(title, label, def, cb) {
  promptTitle.value = title
  promptLabel.value = label
  promptValue.value = def || ''
  promptFn.value = cb
  promptOpen.value = true
}
function confirmPrompt() {
  const v = promptValue.value.trim()
  const fn = promptFn.value
  promptOpen.value = false
  if (fn) fn(v)
}
function promptMkdir() {
  promptInput('新建目录', '目录名称', '', async name => {
    if (!name) return
    try {
      await api.post('/host/files/mkdir', { path: joinPath(curPath.value, name) }, { params: np() })
      toastOk('已创建')
      loadFiles(curPath.value)
    } catch (e) { toastErr(errText(e)) }
  })
}
function promptRename(name) {
  promptInput('重命名', '新名称', name, async nn => {
    if (!nn || nn === name) return
    try {
      await api.post('/host/files/rename',
        { src: joinPath(curPath.value, name), dst: joinPath(curPath.value, nn) }, { params: np() })
      toastOk('已重命名')
      loadFiles(curPath.value)
    } catch (e) { toastErr(errText(e)) }
  })
}
function promptNewFile() {
  promptInput('新建文件', '文件名', '', async name => {
    if (!name) return
    try {
      await api.post('/host/files/save', { path: joinPath(curPath.value, name), content: '' }, { params: np() })
      toastOk('已创建')
      loadFiles(curPath.value)
      openFile(joinPath(curPath.value, name))
    } catch (e) { toastErr(errText(e)) }
  })
}

// ---------- Monaco 编辑（VS Code 内核，Ctrl+S 保存） ----------
const editOpen = ref(false)
const editPath = ref('')
const editContent = ref('')
const editSaved = ref('')
const editSaving = ref(false)
const LANG_NAMES = {
  python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript', json: 'JSON',
  html: 'HTML', css: 'CSS', scss: 'SCSS', less: 'Less', markdown: 'Markdown',
  yaml: 'YAML', xml: 'XML', shell: 'Shell', sql: 'SQL', ini: 'INI', plaintext: '纯文本'
}
const editLangName = computed(() => {
  const ext = (editPath.value.split('.').pop() || '').toLowerCase()
  const m = { py: 'python', js: 'javascript', mjs: 'javascript', ts: 'typescript', json: 'json',
    html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less', md: 'markdown',
    yml: 'yaml', yaml: 'yaml', xml: 'xml', sh: 'shell', bash: 'shell', sql: 'sql',
    ini: 'ini', conf: 'ini', env: 'ini', toml: 'ini', txt: 'plaintext', log: 'plaintext' }
  return LANG_NAMES[m[ext] || 'plaintext'] || '纯文本'
})
const editDirty = computed(() => editContent.value !== editSaved.value)
async function openFile(path) {
  try {
    const { data } = await api.get('/host/files/content', { params: { ...np(), path } })
    editPath.value = path
    editContent.value = data.content ?? ''
    editSaved.value = editContent.value
    editOpen.value = true
  } catch (e) { toastErr(errText(e)) }
}
async function saveEdit() {
  if (editSaving.value || !editPath.value) return
  editSaving.value = true
  try {
    await api.post('/host/files/save', { path: editPath.value, content: editContent.value }, { params: np() })
    editSaved.value = editContent.value
    toastOk('已保存')
    editOpen.value = false
    loadFiles(curPath.value)
  } catch (e) { toastErr(errText(e)) }
  finally { editSaving.value = false }
}

// ---------- 下载 / 图片预览 / 目录大小 ----------
async function downloadFile(name) {
  try {
    const { data } = await api.get('/host/files/download', {
      params: { ...np(), path: joinPath(curPath.value, name) }, responseType: 'blob'
    })
    const url = URL.createObjectURL(data)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) { toastErr(errText(e)) }
}
const previewOpen = ref(false)
const previewUrl = ref('')
const previewName = ref('')
async function previewImg(name) {
  previewName.value = name
  previewUrl.value = ''
  previewOpen.value = true
  try {
    const { data } = await api.get('/host/files/preview',
      { params: { ...np(), path: joinPath(curPath.value, name) } })
    previewUrl.value = data.url
  } catch (e) { previewOpen.value = false; toastErr(errText(e)) }
}
async function calcSize(name) {
  try {
    const { data } = await api.get('/host/files/path-size',
      { params: { ...np(), path: joinPath(curPath.value, name) } })
    dirSizes[name] = fmtSize(data.size)
  } catch { dirSizes[name] = '计算失败' }
}

// ---------- 压缩 / 解压 / 删除 ----------
const zipOpen = ref(false)
const zipType = ref('tar.gz')
const zipName = ref('')
const zipTargets = ref([])
function randomSuffix(n = 6) {
  const c = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
  let s = ''
  for (let i = 0; i < n; i++) s += c[Math.floor(Math.random() * c.length)]
  return s
}
function openZipSel(names) {
  zipTargets.value = names && names.length ? names : [...selection]
  if (!zipTargets.value.length) return
  zipType.value = 'tar.gz'
  const base = zipTargets.value.length > 1 ? 'ALL' : zipTargets.value[0].replace(/\.[^.]+$/, '')
  zipName.value = `${base}_${randomSuffix()}.tar.gz`
  zipOpen.value = true
}
function onZipTypeChange() {
  zipName.value = zipName.value.replace(/\.(tar\.gz|zip)$/, '') + '.' + zipType.value
}
watch(zipType, () => { if (zipOpen.value) onZipTypeChange() })
async function submitZip() {
  const name = zipName.value.trim()
  if (!name) { toastErr('请输入压缩包名'); return }
  try {
    await api.post('/host/files/zip', {
      paths: zipTargets.value.map(n => joinPath(curPath.value, n)),
      name: joinPath(curPath.value, name),
      z_type: zipType.value
    }, { params: np() })
    toastOk('压缩完成')
    zipOpen.value = false
    loadFiles(curPath.value)
  } catch (e) { toastErr(errText(e)) }
}
const unzipOpen = ref(false)
const unzipName = ref('')
function openUnzip(name) {
  unzipName.value = name
  unzipOpen.value = true
}
async function confirmUnzip() {
  const name = unzipName.value
  unzipOpen.value = false
  try {
    await api.post('/host/files/unzip', { src: joinPath(curPath.value, name) }, { params: np() })
    toastOk('解压成功')
    loadFiles(curPath.value)
  } catch (e) { toastErr(errText(e)) }
}
const delOpen = ref(false)
const delList = ref([])
function delItems(names) {
  const list = names && names.length ? names : [...selection]
  if (!list.length) return
  delList.value = list
  delOpen.value = true
}
async function confirmDel() {
  const list = delList.value
  delOpen.value = false
  try {
    for (const name of list) {
      await api.delete('/host/files', { params: { ...np(), path: joinPath(curPath.value, name) } })
    }
    toastOk('删除成功')
    loadFiles(curPath.value)
  } catch (e) { toastErr(errText(e)) }
}

// ---------- 上传弹窗（多文件 + 2MB 分片 + 断点续传 + 进度条） ----------
const CHUNK_SIZE = 2 * 1024 * 1024
const upOpen = ref(false)
const upItems = ref([])
const upUploading = ref(false)
const upSeq = ref(1)
const upInput = ref(null)
function openUploadMask() { upOpen.value = true }
function pickUpFiles() { upInput.value?.click() }
function onPickFiles(e) {
  for (const f of Array.from(e.target.files || [])) {
    if (upItems.value.some(i => i.name === f.name && i.size === f.size)) continue
    upItems.value.push({ id: upSeq.value++, file: f, name: f.name, size: f.size,
      progress: 0, status: 'waiting', resumed: false, error: '' })
  }
  e.target.value = ''
}
function pendingCount() { return upItems.value.filter(f => f.status === 'waiting' || f.status === 'error').length }
function removeUpItem(item) {
  if (item.status === 'uploading') return
  upItems.value = upItems.value.filter(i => i.id !== item.id)
}
async function upOne(item) {
  item.status = 'uploading'
  item.error = ''
  // 断点续传：查询已传偏移
  let startOffset = 0
  try {
    const { data } = await api.post('/host/files/upload-exists',
      { path: curPath.value, file_name: item.name, total_size: item.size }, { params: np() })
    if (data.start > 0 && data.start < item.size) { startOffset = data.start; item.resumed = true }
  } catch { /* 查询失败则从头传 */ }
  item.progress = item.size ? Math.min(99, (startOffset / item.size) * 100) : 0
  try {
    let offset = startOffset
    while (offset < item.size) {
      const end = Math.min(offset + CHUNK_SIZE, item.size)
      const fd = new FormData()
      fd.append('file', item.file.slice(offset, end), item.name)
      fd.append('path', curPath.value)
      fd.append('file_name', item.name)
      fd.append('total_size', String(item.size))
      fd.append('start', String(offset))
      await api.post('/host/files/upload-chunk', fd, { params: np(), timeout: 600000,
        onUploadProgress: ev => {
          if (ev.total) item.progress = Math.min(99, ((offset + ev.loaded) / item.size) * 100)
        } })
      offset = end
      item.progress = Math.min(99, (offset / item.size) * 100)
    }
    item.status = 'success'
    item.progress = 100
    loadFiles(curPath.value)
  } catch (e) {
    item.status = 'error'
    item.error = errText(e)
  }
}
async function startUpload() {
  if (upUploading.value) return
  const queue = upItems.value.filter(f => f.status === 'waiting' || f.status === 'error')
  if (!queue.length) return
  upUploading.value = true
  try {
    for (const item of queue) {
      if (upItems.value.includes(item)) await upOne(item)
    }
    const ok = upItems.value.filter(f => f.status === 'success').length
    toastOk(`上传完成（${ok}/${upItems.value.length}）`)
  } finally {
    upUploading.value = false
  }
}

// ---------- 设置（磁盘占用 / 一键清理 / 镜像加速） ----------
const df = ref(null)
const dfItems = computed(() => df.value ? [
  ['镜像', df.value.images], ['容器', df.value.containers],
  ['数据卷', df.value.volumes], ['构建缓存', df.value.build_cache]
] : [])
const mirrorsText = ref('')
const savingMirrors = ref(false)
const pruneBuilder = ref(false)
const pruneRunning = ref(false)

async function loadDf() {
  try { df.value = (await api.get('/docker/df', { params: np() })).data }
  catch (e) { toastErr(errText(e)) }
}
async function loadSettings() {
  try {
    const { data } = await api.get('/docker/settings', { params: np() })
    mirrorsText.value = (data.registry_mirrors || []).join('\n')
  } catch (e) { toastErr(errText(e)) }
}
async function saveMirrors() {
  const mirrors = mirrorsText.value.split('\n').map(s => s.trim()).filter(Boolean)
  savingMirrors.value = true
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.put('/docker/settings',
      { registry_mirrors: mirrors, restart: true },
      { params: np(), timeout: 120000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '已保存')
  } catch (e) { toastErr(errText(e)) }
  finally { savingMirrors.value = false }
}
async function doSystemPrune() {
  pruneRunning.value = true
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post('/docker/system/prune',
      { builder: pruneBuilder.value }, { params: np(), timeout: 300000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '清理完成')
    loadDf()
  } catch (e) { toastErr(errText(e)) }
  finally { pruneRunning.value = false }
}

// ---------- 打开/关闭 ----------
const navs = [
  { key: 'overview', label: '概览' },
  { key: 'containers', label: '容器' },
  { key: 'images', label: '镜像' },
  { key: 'files', label: '文件管理' },
  { key: 'terminal', label: '宿主机终端' },
  { key: 'firewall', label: '防火墙' },
  { key: 'sec', label: 'SSH 防护' },
  { key: 'mysql', label: 'MySQL 服务' },
  { key: 'backup', label: '备份' },
  { key: 'settings', label: '设置' }
]

watch(() => props.open, v => {
  if (!v) return
  view.value = 'overview'
  ensureIconfont()
  syncPoll()
  loadStability()
})
watch(view, v => {
  if (v === 'containers') loadContainers()
  else if (v === 'images') loadImages()
  else if (v === 'files') { if (!entries.value.length) loadFiles(curPath.value) }
  else if (v === 'settings') { loadDf(); loadSettings() }
  else if (v === 'firewall') loadFirewall()
  else if (v === 'sec') loadSec()
  else if (v === 'mysql') loadMysql()
  else if (v === 'backup') { bkPage.value = 1; loadBackups(); loadBkDbs(); loadBjJobs(); if (!containers.value.length) loadContainers(); loadMysql() }
  syncPoll()
})

// ---------- MySQL 服务（节点级共享 MySQL：每版本一容器，实例一库） ----------
const mysqlRows = ref([])           // [{ version, image, enabled, running, host_port, db_count }]
const mysqlLoading = ref(false)
const mysqlBusy = ref('')           // 正在启用/停用的版本（按钮转圈）
const mysqlDisableOpen = ref(false)
const mysqlDisableVer = ref('')
const mysqlPurgeVol = ref(false)
const mysqlRootOpen = ref(false)
const mysqlRootInfo = ref(null)

async function loadMysql() {
  mysqlLoading.value = true
  try {
    mysqlRows.value = (await api.get('/docker/mysql', { params: np() })).data
  } catch (e) {
    const s = e?.response?.status
    if (s !== 404) toastErr(errText(e))
    mysqlRows.value = []
  } finally {
    mysqlLoading.value = false
  }
}

async function enableMysql(v) {
  mysqlBusy.value = v
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post(`/docker/mysql/${v}/enable`, null,
      { params: np(), timeout: 120000, headers: { 'X-Task-Id': tid } })
    toastOk(`MySQL ${v} 已启用，外网端口 ${data.host_port}`)
    loadMysql()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    mysqlBusy.value = ''
  }
}

function askDisableMysql(v) {
  mysqlDisableVer.value = v
  mysqlPurgeVol.value = false
  mysqlDisableOpen.value = true
}

async function confirmDisableMysql() {
  const v = mysqlDisableVer.value
  mysqlDisableOpen.value = false
  if (!v) return
  mysqlBusy.value = v
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post(`/docker/mysql/${v}/disable`,
      { purge: mysqlPurgeVol.value }, { params: np(), headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '已停用')
    loadMysql()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    mysqlBusy.value = ''
  }
}

async function showRootPwd(v) {
  try {
    mysqlRootInfo.value = (await api.get(`/docker/mysql/${v}/root-password`, { params: np() })).data
    mysqlRootOpen.value = true
  } catch (e) { toastErr(errText(e)) }
}

// ---------- 备份（容器导出 / 实例数据库导出） ----------
const backups = ref([])            // [{ file, kind, size, created_at }]
const backupsLoading = ref(false)
const bkTotal = ref(0)
const bkPage = ref(1)
const bkPageSize = ref(20)
const bkDbs = ref([])              // [{ version, db_name, running }]
const bkContainer = ref('')        // 选中要备份的容器 id
const bkDb = ref('')               // 选中 "version|db_name"
const bkBusy = ref('')             // 'c' 容器备份中 / 'd' 数据库备份中 / 'w' 目录备份中
const bkDelOpen = ref(false)
const bkDelFile = ref('')
const bkDirPath = ref('')          // 网站目录备份：容器内路径
const bkTables = ref([])           // 表级备份：已选表（空 = 整库）
const bkTableOpts = ref([])        // 选中库的表列表
const bkTblLoading = ref(false)

const bkContainerOpts = computed(() => containers.value
  .filter(c => c.state === 'running')
  .map(c => ({ value: c.id, label: `${c.name}（${c.image}）` })))
const bkDbOpts = computed(() => bkDbs.value
  .map(d => ({ value: `${d.version}|${d.db_name}`,
               label: `${d.db_name}（MySQL ${d.version}${d.running ? '' : '，服务未运行'}）` })))

// 恢复面板
const bkRestore = ref(null)        // { file, kind } 当前恢复目标
const bkRestoreName = ref('')      // 容器恢复：新容器名
const bkRestoreVer = ref('')       // 数据库恢复：目标 MySQL 版本
const bkRestoreDb = ref('')        // 数据库恢复：目标库名
const bkRestoreCid = ref('')       // 目录恢复：目标容器 id
const bkRestorePath = ref('')      // 目录恢复：目标目录（容器内）
const bkRestoreBusy = ref(false)
const bkVerOpts = computed(() => mysqlRows.value
  .filter(x => x.enabled && x.running)
  .map(x => ({ value: x.version, label: `MySQL ${x.version}` })))

async function loadBackups() {
  backupsLoading.value = true
  try {
    const { data } = await api.get('/host/backups', {
      params: { ...np(), page: bkPage.value, page_size: bkPageSize.value }
    })
    backups.value = data.backups || []
    bkTotal.value = data.total || 0
    // 删除后当前页空了 → 回退到最后一页
    if (!backups.value.length && bkPage.value > 1 && bkTotal.value > 0) {
      bkPage.value = Math.ceil(bkTotal.value / bkPageSize.value)
      return loadBackups()
    }
  } catch (e) {
    const s = e?.response?.status
    if (s !== 404) toastErr(errText(e))
    backups.value = []
  } finally {
    backupsLoading.value = false
  }
}

async function loadBkDbs() {
  try {
    const { data } = await api.get('/host/backups/dbs', { params: np() })
    bkDbs.value = data.dbs || []
  } catch (e) {
    const s = e?.response?.status
    if (s !== 404) toastErr(errText(e))
    bkDbs.value = []
  }
}

async function backupContainer() {
  if (!bkContainer.value) return
  bkBusy.value = 'c'
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post('/host/backups/container',
      { container_id: bkContainer.value },
      { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '容器备份完成')
    loadBackups()
  } catch (e) { toastErr(errText(e)) }
  finally { bkBusy.value = '' }
}

async function backupDatabase() {
  if (!bkDb.value) return
  const [version, db_name] = bkDb.value.split('|')
  bkBusy.value = 'd'
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post('/host/backups/database',
      { version, db_name, tables: bkTables.value },
      { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '数据库备份完成')
    loadBackups()
  } catch (e) { toastErr(errText(e)) }
  finally { bkBusy.value = '' }
}

async function backupDir() {
  if (!bkContainer.value) return
  bkBusy.value = 'w'
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post('/host/backups/dir',
      { container_id: bkContainer.value, path: bkDirPath.value.trim() || '/app' },
      { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '目录备份完成')
    loadBackups()
  } catch (e) { toastErr(errText(e)) }
  finally { bkBusy.value = '' }
}

async function loadTables() {
  bkTables.value = []
  bkTableOpts.value = []
  if (!bkDb.value) return
  const [version, db_name] = bkDb.value.split('|')
  bkTblLoading.value = true
  try {
    const { data } = await api.get('/host/backups/tables',
      { params: { ...np(), version, db_name } })
    bkTableOpts.value = data.tables || []
  } catch (e) {
    bkTableOpts.value = []
  } finally { bkTblLoading.value = false }
}

// ---------- 定时备份任务（管理员 cron：容器 / 目录 / 数据库整库或表级） ----------
const bjJobs = ref([])
const bjLoading = ref(false)
const bjOpen = ref(false)
const bjSchedule = ref('0 3 * * *')
const bjKind = ref('db_full')
const bjContainer = ref('')
const bjDirPath = ref('/app')
const bjDb = ref('')               // "version|db_name"
const bjTable = ref('')            // 表级：单选表（空 = 整库）
const bjTableOpts = ref([])
const bjKeep = ref(5)
const bjBusy = ref(false)
const bjMeta = ref({ containers: [], mysql: [] })

const BJ_KIND_LABEL = { container: '容器', dir: '目录', db_full: '数据库整库', db_table: '数据库表级' }
const BJ_PRESETS = [
  { value: '0 3 * * *', label: '每天 03:00' },
  { value: '0 4 * * 1', label: '每周一 04:00' },
  { value: '0 */6 * * *', label: '每 6 小时' },
  { value: '30 2 1 * *', label: '每月 1 日 02:30' },
]
const bjKindOpts = Object.entries(BJ_KIND_LABEL).map(([value, label]) => ({ value, label }))
const bjContainerOpts = computed(() => (bjMeta.value.containers || [])
  .map(c => ({ value: c.name, label: `${c.name}（${c.image}${c.status === 'running' ? '' : '，已停止'}）` })))
const bjDbOpts = computed(() => (bjMeta.value.mysql || []).filter(m => m.running).flatMap(m =>
  m.dbs.map(d => ({ value: `${m.version}|${d}`, label: `${d}（MySQL ${m.version}）` }))))
const canSaveBj = computed(() => {
  if (!bjSchedule.value.trim()) return false
  if (bjKind.value === 'container' || bjKind.value === 'dir') return !!bjContainer.value
  return !!bjDb.value
})
function bjSchedLabel(s) { return (BJ_PRESETS.find(p => p.value === s) || {}).label || s }
function bjTargetLabel(j) {
  if (j.kind === 'container' || j.kind === 'dir')
    return `${j.target}:${j.dir_path || '/app'}`
  return j.kind === 'db_table' && j.table_name ? `${j.target.split(':')[1]}.${j.table_name}` : j.target.replace(':', ' / ')
}
function fmtISO(s) {
  if (!s) return ''
  const d = new Date(s.endsWith('Z') ? s : s + 'Z')  // 被控存 UTC
  const p = x => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function loadBjJobs() {
  bjLoading.value = true
  try {
    const [jobsR, metaR] = await Promise.all([
      api.get('/host/backup-jobs', { params: np() }),
      api.get('/host/backup-jobs/meta', { params: np() }).catch(() => null),
    ])
    bjJobs.value = jobsR.data.jobs || []
    if (metaR) bjMeta.value = metaR.data
  } catch (e) {
    const s = e?.response?.status
    if (s !== 404) toastErr(errText(e))
    bjJobs.value = []
  } finally { bjLoading.value = false }
}

watch(bjDb, async v => {
  bjTable.value = ''
  bjTableOpts.value = []
  if (!v) return
  const [version, db_name] = v.split('|')
  try {
    const { data } = await api.get('/host/backups/tables', { params: { ...np(), version, db_name } })
    bjTableOpts.value = data.tables || []
  } catch { bjTableOpts.value = [] }
})

async function saveBjJob() {
  bjBusy.value = true
  try {
    const payload = { schedule: bjSchedule.value.trim(), kind: bjKind.value, keep: bjKeep.value || 5 }
    if (bjKind.value === 'container' || bjKind.value === 'dir') {
      payload.target = bjContainer.value
      payload.dir_path = bjDirPath.value.trim() || '/app'
    } else {
      const [version, db_name] = bjDb.value.split('|')
      payload.target = `${version}:${db_name}`
      payload.table_name = bjKind.value === 'db_table' ? bjTable.value : ''
    }
    const { data } = await api.post('/host/backup-jobs', payload, { params: np() })
    toastOk(`定时任务已创建（${BJ_KIND_LABEL[data.kind]}），首份备份执行中`)
    bjOpen.value = false
    loadBjJobs()
    loadBackups()
  } catch (e) { toastErr(errText(e)) }
  finally { bjBusy.value = false }
}

async function toggleBjJob(j) {
  try {
    await api.patch(`/host/backup-jobs/${j.id}`, { enabled: !j.enabled }, { params: np() })
    j.enabled = !j.enabled
    toastOk(j.enabled ? '任务已启用' : '任务已暂停')
  } catch (e) { toastErr(errText(e)) }
}

async function runBjJob(j) {
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post(`/host/backup-jobs/${j.id}/run`, {},
      { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '执行完成')
    loadBjJobs()
    loadBackups()
  } catch (e) { toastErr(errText(e)); loadBjJobs() }
}

async function delBjJob(j) {
  try {
    await api.delete(`/host/backup-jobs/${j.id}`, { params: np() })
    toastOk('定时任务已删除')
    loadBjJobs()
  } catch (e) { toastErr(errText(e)) }
}

function toggleTable(t) {
  const i = bkTables.value.indexOf(t)
  if (i >= 0) bkTables.value.splice(i, 1)
  else bkTables.value.push(t)
}

watch(bkDb, loadTables)

function askDelBackup(file) {
  bkDelFile.value = file
  bkDelOpen.value = true
}

function startRestore(b) {
  bkRestore.value = b
  if (b.kind === 'container') {
    bkRestoreName.value = 'restore-' + b.file.replace(/\.tar$/, '')
  } else if (b.kind === 'dir') {
    bkRestoreCid.value = bkContainer.value || ''
    bkRestorePath.value = ''
  } else {
    bkRestoreDb.value = b.file.replace(/\.sql\.gz$/, '')
    const running = mysqlRows.value.filter(x => x.enabled && x.running)
    bkRestoreVer.value = running[0]?.version || ''
    if (!running.length) toastErr('当前没有运行中的 MySQL 服务，请先在「MySQL 服务」启用')
  }
}

async function doRestore() {
  const t = bkRestore.value
  if (!t) return
  bkRestoreBusy.value = true
  try {
    const tid = startTask(props.node?.id)
    const { data } = t.kind === 'container'
      ? await api.post('/host/backups/restore/container',
          { file: t.file, name: bkRestoreName.value.trim() },
          { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
      : t.kind === 'dir'
      ? await api.post('/host/backups/restore/dir',
          { file: t.file, container_id: bkRestoreCid.value, path: bkRestorePath.value.trim() },
          { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
      : await api.post('/host/backups/restore/database',
          { file: t.file, version: bkRestoreVer.value, db_name: bkRestoreDb.value.trim() },
          { params: np(), timeout: 600000, headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '恢复完成')
    bkRestore.value = null
  } catch (e) { toastErr(errText(e)) }
  finally { bkRestoreBusy.value = false }
}

async function doDelBackup() {
  try {
    const { data } = await api.delete('/host/backups',
      { params: { ...np(), file: bkDelFile.value } })
    toastOk(data.detail || '已删除')
    bkDelOpen.value = false  // 删除成功自动关确认弹窗
    loadBackups()
  } catch (e) { toastErr(errText(e)) }
}

async function downloadBackup(file) {
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

// ---------- 防火墙 ----------
const fwCols = [
  { key: 'num', label: '#', width: '52px' },
  { key: 'port', label: '端口' },
  { key: 'action', label: '动作', width: '90px' },
  { key: 'from', label: '来源', width: '170px' },
  { key: 'ops', label: '', width: '70px' }
]
const fw = ref(null)          // { tool, active, rules, error }
const fwLoading = ref(false)
const fwPortOpen = ref(false)
const fwPort = ref('')
const fwProto = ref('tcp')
const fwAction = ref('allow')  // allow / deny
const fwSaving = ref(false)
const fwToggleOpen = ref(false)

async function loadFirewall() {
  fwLoading.value = true
  try {
    const { data } = await api.get('/host/firewall', { params: np() })
    fw.value = data
  } catch (e) {
    const s = e?.response?.status
    fw.value = {
      tool: null, active: false, rules: [],
      error: s === 404 ? '该节点的被控端代码版本较旧，尚无防火墙接口，请同步最新 agent.py 后重启被控服务' : errText(e)
    }
  } finally {
    fwLoading.value = false
  }
}

async function saveFwRule() {
  if (!/^\d{1,5}(:\d{1,5})?$/.test(fwPort.value.trim())) { toastErr('端口格式：8080 或 30000:39999'); return }
  fwSaving.value = true
  try {
    const { data } = await api.post(`/host/firewall/${fwAction.value}`,
      { port: fwPort.value.trim(), proto: fwProto.value }, { params: np() })
    toastOk(data.detail || '已生效')
    fwPortOpen.value = false
    fwPort.value = ''
    loadFirewall()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    fwSaving.value = false
  }
}

async function delFwRule(r) {
  const [p, pr] = (r.port || '/tcp').split('/')
  try {
    const { data } = await api.post('/host/firewall/delete',
      { nums: r.nums || [], num: r.num || 0, port: p || '', proto: pr || 'tcp' },
      { params: np() })
    toastOk(data.detail || '规则已删除')
    loadFirewall()
  } catch (e) {
    toastErr(errText(e))
  }
}

async function doFwToggle() {
  const enable = !(fw.value && fw.value.active)
  try {
    const { data } = await api.post('/host/firewall/toggle', { enable }, { params: np() })
    toastOk(data.detail || '已切换')
    fwToggleOpen.value = false
    loadFirewall()
  } catch (e) {
    toastErr(errText(e))
    fwToggleOpen.value = false
  }
}

// ---------- SSH 防护（fail2ban 防爆破） ----------
const sec = ref(null)   // { installed, active, version, jails, sshd, error }
const secLoading = ref(false)
const secBusy = ref(false)
const secBanIp = ref('')
const secUnbanOpen = ref(false)
const secUnbanIp = ref('')
const secToggleOpen = ref(false)

async function loadSec() {
  secLoading.value = true
  try {
    const { data } = await api.get('/security/fail2ban', { params: np() })
    sec.value = data
  } catch (e) {
    const s = e?.response?.status
    sec.value = {
      installed: false, active: false, version: '', jails: [], sshd: null,
      error: s === 404 ? '该节点的被控端代码版本较旧，尚无安全防护接口，请同步最新 agent 后重启被控服务' : errText(e)
    }
  } finally {
    secLoading.value = false
  }
}

async function secSetup() {
  secBusy.value = true
  try {
    const tid = startTask(props.node?.id)
    const { data } = await api.post('/security/fail2ban/setup', null,
      { params: np(), timeout: 180000, headers: { 'X-Task-Id': tid } })  // 包安装最长可达数分钟
    sec.value = data
    toastOk('fail2ban 已安装并启用')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    secBusy.value = false
  }
}

async function doSecToggle() {
  const enable = !(sec.value && sec.value.active)
  try {
    const { data } = await api.post('/security/fail2ban/toggle', { enable }, { params: np() })
    sec.value = data
    toastOk(enable ? 'fail2ban 已启用' : 'fail2ban 已停用')
    secToggleOpen.value = false
  } catch (e) {
    toastErr(errText(e))
    secToggleOpen.value = false
  }
}

async function secBan() {
  const ip = secBanIp.value.trim()
  if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(ip) && !/^[0-9A-Fa-f:]{2,45}$/.test(ip)) {
    toastErr('IP 格式无效'); return
  }
  secBusy.value = true
  try {
    const { data } = await api.post('/security/fail2ban/ban', { ip }, { params: np() })
    toastOk(data.detail || '已封禁')
    secBanIp.value = ''
    loadSec()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    secBusy.value = false
  }
}

async function doSecUnban() {
  try {
    const { data } = await api.post('/security/fail2ban/unban', { ip: secUnbanIp.value }, { params: np() })
    toastOk(data.detail || '已解封')
    secUnbanOpen.value = false
    loadSec()
  } catch (e) {
    toastErr(errText(e))
    secUnbanOpen.value = false
  }
}

onMounted(() => {
  ensureIconfont()
  document.addEventListener('mousedown', onDocMouseDown)
  document.addEventListener('keydown', onGlobalKey)
  window.addEventListener('scroll', closeCtx, true)
  window.addEventListener('resize', closeCtx)
})
onUnmounted(() => {
  if (statTimer) clearInterval(statTimer)
  document.removeEventListener('mousedown', onDocMouseDown)
  document.removeEventListener('keydown', onGlobalKey)
  window.removeEventListener('scroll', closeCtx, true)
  window.removeEventListener('resize', closeCtx)
})
</script>

<template>
  <UIModal :open="open" width="94vw" :title="`节点管理 - ${node?.name || ''}`"
           @update:open="emit('update:open', $event)">
    <div class="nam-body">
      <aside class="nam-side">
        <button v-for="n in navs" :key="n.key" class="nav-item" :class="{ active: view === n.key }"
                @click="view = n.key">
          <span>{{ n.label }}</span>
          <i v-if="view === n.key" class="ind" />
        </button>
      </aside>

      <section class="nam-main">
        <!-- 概览（资源仪表盘） -->
        <div v-if="view === 'overview'">
          <div class="dash-row">
            <div v-for="g in gauges" :key="g.label" class="gauge-card">
              <div class="gauge-wrap">
                <svg viewBox="0 0 120 120">
                  <circle class="g-bg" cx="60" cy="60" r="52" />
                  <circle class="g-val" cx="60" cy="60" r="52" :stroke="g.color"
                          :stroke-dasharray="RING_C" :stroke-dashoffset="RING_C * (1 - (g.p || 0) / 100)" />
                </svg>
                <div class="g-center">
                  <div class="g-num mono">{{ g.p == null ? '-' : g.p + '%' }}</div>
                  <div class="g-label">{{ g.label }}</div>
                </div>
              </div>
              <div class="g-sub mono" :title="g.sub">{{ g.sub }}</div>
            </div>
          </div>
          <div v-if="info" class="ov-grid">
            <div v-for="[k, v] in infoItems" :key="k" class="ov-cell">
              <span class="k">{{ k }}</span><span class="v mono">{{ v }}</span>
            </div>
          </div>
          <p class="hint-text">资源数据每 5 秒自动刷新。</p>

          <!-- 稳定性评分 -->
          <div class="stab-card">
            <div class="bar">
              <span class="hint-text">稳定性评分</span>
              <div class="win-chips">
                <button v-for="w in [1, 7, 30]" :key="w" class="win-chip"
                        :class="{ active: stabWin === w }" @click="stabWin = w; loadStability()">
                  {{ w === 1 ? '24小时' : w + '天' }}
                </button>
              </div>
            </div>
            <template v-if="stab">
              <div class="stab-head">
                <span class="stab-score mono" :class="'st-' + stab.grade">{{ stab.score != null ? stab.score : '--' }}</span>
                <div class="stab-meta">
                  <UITag :tone="stabTone(stab.grade)">{{ stab.grade_label }}</UITag>
                  <span class="dim">近 {{ stab.window_days }} 天 · {{ stab.instances }} 个实例</span>
                </div>
                <div class="stab-parts">
                  <div class="sp"><i>崩溃</i><b class="mono">{{ stab.parts.crash ?? '-' }}</b></div>
                  <div class="sp"><i>在线</i><b class="mono">{{ stab.parts.online ?? '-' }}</b></div>
                  <div class="sp"><i>资源</i><b class="mono">{{ stab.parts.resource ?? '-' }}</b></div>
                  <div class="sp"><i>服务</i><b class="mono">{{ stab.parts.service ?? '-' }}</b></div>
                </div>
              </div>
              <div class="stab-facts mono dim">
                <span>崩溃 {{ stab.crashes }} 次</span>
                <span>在线率 {{ stab.online_rate != null ? stab.online_rate + '%' : '--' }}</span>
                <span>内存峰值 {{ stab.mem_peak != null ? stab.mem_peak + '%' : '--' }}</span>
                <span>磁盘峰值 {{ stab.disk_peak != null ? stab.disk_peak + '%' : '--' }}</span>
                <span>最近崩溃 {{ stab.last_crash_at ? fmtIso(stab.last_crash_at) : '无' }}</span>
              </div>
            </template>
            <p v-else class="hint-text">评分数据加载中…</p>
          </div>
        </div>

        <!-- 容器 -->
        <div v-else-if="view === 'containers'">
          <div class="bar">
            <span class="hint-text">共 {{ containers.length }} 个容器</span>
            <UIButton type="ghost" @click="loadContainers">刷新</UIButton>
          </div>
          <table class="ftable">
            <thead><tr><th>名称</th><th>镜像</th><th>状态</th><th>端口</th><th /></tr></thead>
            <tbody>
              <tr v-for="c in containers" :key="c.id">
                <td class="mono">{{ c.name }} <UITag v-if="c.is_panel" tone="primary">面板实例</UITag></td>
                <td class="mono dim">{{ c.image }}</td>
                <td><StatusDot :status="c.state" /></td>
                <td class="mono dim">{{ c.ports }}</td>
                <td class="ops">
                  <UIButton v-if="c.state !== 'running'" type="text" @click="contAction(c, 'start')">启动</UIButton>
                  <template v-else>
                    <UIButton type="text" @click="contAction(c, 'stop')">停止</UIButton>
                    <UIButton type="text" @click="contAction(c, 'restart')">重启</UIButton>
                  </template>
                  <UIButton type="text" @click="showLogs(c)">日志</UIButton>
                  <UIButton type="text" class="danger" @click="askDelContainer(c)">删除</UIButton>
                </td>
              </tr>
              <tr v-if="!containers.length"><td colspan="5" class="empty">暂无容器</td></tr>
            </tbody>
          </table>
        </div>

        <!-- 镜像 -->
        <div v-else-if="view === 'images'">
          <div class="bar">
            <span class="hint-text">共 {{ images.length }} 个镜像</span>
            <span class="sp" />
            <UIButton type="ghost" :disabled="pruning" :loading="pruning" @click="pruneImages">清理悬空镜像</UIButton>
            <UIButton type="ghost" @click="loadImages">刷新</UIButton>
            <UIButton type="ghost" @click="askPhpBuild">构建 PHP 增强镜像</UIButton>
            <UIButton @click="pullOpen = true">拉取镜像</UIButton>
          </div>
          <table class="ftable">
            <thead><tr><th>镜像</th><th>大小</th><th /></tr></thead>
            <tbody>
              <tr v-for="img in images" :key="img.full_name">
                <td class="mono">{{ img.full_name }}</td>
                <td class="mono dim">{{ img.size_mb }} MB</td>
                <td class="ops">
                  <UIButton type="text" class="danger" @click="askDelImage(img)">删除</UIButton>
                </td>
              </tr>
              <tr v-if="!images.length"><td colspan="3" class="empty">暂无镜像</td></tr>
            </tbody>
          </table>
        </div>

        <!-- 文件管理（独立面板同款） -->
        <div v-else-if="view === 'files'" class="fm" @contextmenu="blankMenu">
          <div class="fm-bar">
            <button class="tool" @click="go(parentOf(curPath))">上级</button>
            <button class="tool" @click="loadFiles(curPath)">刷新</button>
            <div class="crumbs mono">
              <template v-for="(c, i) in crumbs" :key="c.path">
                <span v-if="i" class="sep">/</span>
                <button class="crumb" @click="go(c.path)">{{ c.name }}</button>
              </template>
            </div>
            <span v-if="clip" class="clip-tip">已{{ clip.mode === 'cut' ? '剪切' : '复制' }} {{ clip.items.length }} 项</span>
            <button v-if="clip" class="tool" @click="doPaste">粘贴</button>
            <button class="tool" @click="promptMkdir">新建目录</button>
            <button class="tool" @click="promptNewFile">新建文件</button>
            <button class="tool" @click="openUploadMask">上传</button>
            <input ref="upInput" type="file" multiple hidden @change="onPickFiles">
          </div>

          <!-- 选中操作栏 -->
          <div v-if="selection.size" class="sel-bar">
            <span class="sel-count">已选 <b>{{ selection.size }}</b> 项</span>
            <div class="sel-actions">
              <button class="sbtn" @click="clipCopy()">复制</button>
              <button class="sbtn" @click="clipCut()">剪切</button>
              <button class="sbtn" @click="openZipSel()">压缩</button>
              <button class="sbtn danger" @click="delItems([...selection])">删除</button>
            </div>
            <button class="sel-cancel" @click="clearSel">取消选择</button>
          </div>

          <div class="fscroll">
            <table class="ftable" :key="iconTick">
              <thead>
                <tr>
                  <th style="width:36px">
                    <span class="ck" :class="{ on: entries.length && selection.size === entries.length, ind: selection.size && selection.size < entries.length }"
                          @click.stop="toggleAll">
                      <span v-if="selection.size && selection.size < entries.length" class="ck-ind" />
                      <span v-else class="ck-box" v-html="biSvg('check')" />
                    </span>
                  </th>
                  <th>名称</th>
                  <th style="width:110px">大小</th>
                  <th style="width:150px">修改时间</th>
                  <th style="width:180px">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="it in entries" :key="it.name" :data-kind="it.is_dir ? 'dir' : 'file'"
                    :class="{ selected: selection.has(it.name) }"
                    @dblclick="it.is_dir ? go(joinPath(curPath, it.name)) : (isImage(it.name) ? previewImg(it.name) : (isZip(it.name) ? openUnzip(it.name) : openFile(joinPath(curPath, it.name))))"
                    @contextmenu="rowMenu($event, it)">
                  <td>
                    <span class="ck" :class="{ on: selection.has(it.name) }" @click.stop="toggleRow(it.name)">
                      <span class="ck-box" v-html="biSvg('check')" />
                    </span>
                  </td>
                  <td>
                    <span class="name-cell">
                      <svg class="f-svg" aria-hidden="true"><use :xlink:href="'#' + fileIconId(it.name, it.is_dir)" /></svg>
                      <span class="fname-text">{{ it.name }}</span>
                    </span>
                  </td>
                  <td class="mono dim">
                    <a v-if="it.is_dir && dirSizes[it.name] === undefined" class="size-link" @click.stop.prevent="calcSize(it.name)">计算</a>
                    <template v-else>{{ it.is_dir ? (dirSizes[it.name] || '-') : fmtSize(it.size) }}</template>
                  </td>
                  <td class="mono dim">{{ fmtTime(it.mtime) }}</td>
                  <td>
                    <span class="table-actions">
                      <button v-if="!it.is_dir && isImage(it.name)" class="tbtn" title="预览" @click.stop="previewImg(it.name)" v-html="biSvg('eye')" />
                      <button v-if="!it.is_dir && editable(it.name)" class="tbtn" title="编辑" @click.stop="openFile(joinPath(curPath, it.name))" v-html="biSvg('edit')" />
                      <button v-if="!it.is_dir" class="tbtn" title="下载" @click.stop="downloadFile(it.name)" v-html="biSvg('download')" />
                      <button v-if="!it.is_dir && isZip(it.name)" class="tbtn" title="解压" @click.stop="openUnzip(it.name)" v-html="biSvg('unzip')" />
                      <button class="tbtn" title="压缩" @click.stop="openZipSel([it.name])" v-html="biSvg('zip')" />
                      <button v-if="!it.is_dir && !isImage(it.name) && !isZip(it.name)" class="tbtn" title="重命名" @click.stop="promptRename(it.name)" v-html="biSvg('edit')" />
                      <button class="tbtn danger" title="删除" @click.stop="delItems([it.name])" v-html="biSvg('trash')" />
                    </span>
                  </td>
                </tr>
                <tr v-if="!entries.length && !fLoading"><td colspan="5" class="empty">此目录为空（右键可新建）</td></tr>
              </tbody>
            </table>
          </div>
          <p class="hint-text">单击选中，Ctrl 多选，Shift 范围选；右键打开菜单；双击进入目录 / 打开文件。</p>
        </div>

        <!-- 宿主机终端 -->
        <div v-else-if="view === 'terminal'" class="term-slot">
          <HostTerminal :node-id="String(node?.id || '')" />
        </div>

        <!-- 防火墙：ufw / firewalld 端口规则管理 -->
        <div v-else-if="view === 'firewall'">
          <div class="fw-head">
            <span class="fw-tool">
              <template v-if="fw && fw.tool">
                {{ fw.tool }} <i class="fw-dot" :class="fw.active ? 'on' : 'off'" />
                <em>{{ fw.active ? '启用中' : '未启用' }}</em>
              </template>
              <template v-else>未检测到防火墙工具</template>
            </span>
            <span class="sp" />
            <UIButton type="ghost" size="sm" :loading="fwLoading" @click="loadFirewall">刷新</UIButton>
            <UIButton v-if="fw && fw.tool" type="ghost" size="sm" @click="fwToggleOpen = true">
              {{ fw.active ? '停用防火墙' : '启用防火墙' }}
            </UIButton>
            <UIButton size="sm" :disabled="!fw || !fw.tool" @click="fwPortOpen = true">开放端口</UIButton>
          </div>

          <div v-if="fw && !fw.tool" class="set-card fw-none">
            <p class="form-tip">节点未安装 ufw / firewalld。可在宿主机终端执行安装：<br>
              <code>apt install -y ufw && ufw allow 9100/tcp && ufw enable</code><br>
              或 <code>yum install -y firewalld && systemctl enable --now firewalld</code></p>
            <UIButton type="ghost" size="sm" @click="loadFirewall">重新检测</UIButton>
          </div>

          <p v-else-if="fw && fw.error" class="form-tip danger-tip">{{ fw.error }}</p>

          <UITable v-else :columns="fwCols" :rows="fw ? fw.rules : []" :loading="fwLoading">
            <template #col-port="{ row }"><span class="mono">{{ row.port }}</span></template>
            <template #col-action="{ row }">
              <i class="fw-badge" :class="row.action === 'ALLOW' ? 'allow' : 'deny'">{{ row.action }}</i>
            </template>
            <template #col-ops="{ row }">
              <UIButton type="text" class="danger" @click="delFwRule(row)">删除</UIButton>
            </template>
          </UITable>
        </div>

        <!-- SSH 防护：fail2ban 防爆破（安装/启停/封禁/解封） -->
        <div v-else-if="view === 'sec'">
          <div class="fw-head">
            <span class="fw-tool">
              <template v-if="sec && sec.installed">
                fail2ban <i class="fw-dot" :class="sec.active ? 'on' : 'off'" />
                <em>{{ sec.active ? `运行中（${sec.version || ''}）` : '未运行' }}</em>
              </template>
              <template v-else>未安装 fail2ban</template>
            </span>
            <span class="sp" />
            <UIButton type="ghost" size="sm" :loading="secLoading" @click="loadSec">刷新</UIButton>
            <UIButton v-if="sec && sec.installed" type="ghost" size="sm" @click="secToggleOpen = true">
              {{ sec.active ? '停用' : '启用' }}
            </UIButton>
            <UIButton size="sm" :loading="secBusy" @click="secSetup">
              {{ sec && sec.installed ? '重装 / 修复' : '安装并启用' }}
            </UIButton>
          </div>

          <p v-if="sec && sec.error && !sec.installed" class="form-tip danger-tip">{{ sec.error }}</p>
          <div v-else-if="sec && !sec.installed" class="set-card fw-none">
            <p class="form-tip">fail2ban 监控 SSH 登录失败：同一 IP 短时间内失败 5 次将自动封禁 10 分钟，防御 SSH 爆破。<br>
              点击「安装并启用」将在该节点上安装 fail2ban 并写入策略（约 1 分钟，不影响已有防护配置）。</p>
          </div>

          <template v-else-if="sec && sec.installed">
            <p v-if="sec.error" class="form-tip danger-tip">{{ sec.error }}</p>
            <div v-if="sec.active && sec.sshd && sec.sshd.enabled" class="sec-stats">
              <div class="sec-stat"><b>{{ sec.sshd.currently_failed }}</b><span>当前失败</span></div>
              <div class="sec-stat"><b>{{ sec.sshd.total_failed }}</b><span>累计失败</span></div>
              <div class="sec-stat"><b :class="{ hot: sec.sshd.currently_banned > 0 }">{{ sec.sshd.currently_banned }}</b><span>当前封禁</span></div>
              <div class="sec-stat"><b>{{ sec.sshd.total_banned }}</b><span>累计封禁</span></div>
            </div>

            <div v-if="sec.active" class="set-card" style="margin-top:12px">
              <div class="fw-head" style="padding:0 0 10px">
                <span class="fw-tool"><em>SSH 封禁列表（sshd jail）</em></span>
                <span class="sp" />
                <UIInput v-model="secBanIp" placeholder="手动封禁 IP" style="width:180px" @keyup.enter="secBan" />
                <UIButton size="sm" :loading="secBusy" :disabled="!secBanIp.trim()" @click="secBan">封禁</UIButton>
              </div>
              <p v-if="!sec.sshd || !sec.sshd.enabled" class="form-tip danger-tip">
                {{ (sec.sshd && sec.sshd.error) || 'sshd jail 未启用，请点「重装 / 修复」' }}</p>
              <p v-else-if="!sec.sshd.banned.length" class="form-tip">暂无封禁中的 IP（出现 SSH 爆破时自动加入）</p>
              <div v-else class="sec-bans">
                <span v-for="ip in sec.sshd.banned" :key="ip" class="sec-ban-ip mono">
                  {{ ip }}
                  <i title="解封" @click="secUnbanIp = ip; secUnbanOpen = true">×</i>
                </span>
              </div>
            </div>
          </template>
        </div>

        <!-- MySQL 服务：节点级共享 MySQL（每版本一容器，实例一库） -->
        <div v-else-if="view === 'mysql'">
          <div class="bar">
            <span class="hint-text">启用后实例可在独立面板「数据库」页一键开通专属库（内网容器名直连，外网端口远程管理）</span>
            <span class="sp" />
            <UIButton type="ghost" :loading="mysqlLoading" @click="loadMysql">刷新</UIButton>
          </div>
          <table class="ftable">
            <thead><tr><th>版本</th><th>镜像</th><th>状态</th><th>外网端口</th><th>实例库</th><th /></tr></thead>
            <tbody>
              <tr v-for="r in mysqlRows" :key="r.version">
                <td class="mono">MySQL {{ r.version }}</td>
                <td class="mono dim">{{ r.image }}</td>
                <td>
                  <template v-if="r.enabled"><StatusDot :status="r.running ? 'running' : 'exited'" /> {{ r.running ? '运行中' : '已停止' }}</template>
                  <span v-else class="dim">未启用</span>
                </td>
                <td class="mono">{{ r.enabled ? (r.host_port || '-') : '-' }}</td>
                <td>{{ r.db_count }} 个</td>
                <td class="ops">
                  <template v-if="r.enabled">
                    <UIButton v-if="r.running" type="text" @click="showRootPwd(r.version)">root 密码</UIButton>
                    <UIButton type="text" class="danger" :loading="mysqlBusy === r.version"
                              @click="askDisableMysql(r.version)">停用</UIButton>
                  </template>
                  <UIButton v-else type="text" :loading="mysqlBusy === r.version"
                            @click="enableMysql(r.version)">启用</UIButton>
                </td>
              </tr>
              <tr v-if="!mysqlRows.length"><td colspan="6" class="empty">暂无可选版本</td></tr>
            </tbody>
          </table>
          <p class="form-tip" style="margin-top:10px">
            启用要求镜像已拉取（绝不自动拉取；mysql:5.7 无 ARM64 版本，树莓派节点不可用）。
            停用时需先清空该版本下所有实例数据库；数据卷默认保留，勾选「同时删除数据卷」才彻底清除。
          </p>
        </div>

        <!-- 备份：容器导出 / 实例数据库导出 -->
        <div v-else-if="view === 'backup'">
          <div class="set-card">
            <div class="bk-line">
              <span class="bk-label">容器备份</span>
              <UISelect v-model="bkContainer" style="width:280px" :options="bkContainerOpts"
                        placeholder="选择要备份的容器" />
              <UIButton size="sm" :loading="bkBusy === 'c'" :disabled="!bkContainer"
                        @click="backupContainer">备份容器</UIButton>
            </div>
            <div class="bk-line">
              <span class="bk-label">目录备份</span>
              <UIInput v-model="bkDirPath" style="width:280px" class="mono"
                       placeholder="容器内网站目录，留空默认 /app" />
              <UIButton size="sm" :loading="bkBusy === 'w'" :disabled="!bkContainer"
                        @click="backupDir">备份目录</UIButton>
            </div>
            <div class="bk-line">
              <span class="bk-label">数据库</span>
              <UISelect v-model="bkDb" style="width:280px" :options="bkDbOpts"
                        placeholder="选择要备份的数据库" />
              <UIButton size="sm" :loading="bkBusy === 'd'" :disabled="!bkDb"
                        @click="backupDatabase">备份数据库</UIButton>
            </div>
            <div v-if="bkDb" class="bk-line" style="padding-left:70px">
              <template v-if="bkTableOpts.length">
                <span class="hint-text" style="font-size:12px">表级（不选 = 整库）：</span>
                <span v-for="t in bkTableOpts" :key="t" class="tbl-chip"
                      :class="{ on: bkTables.includes(t) }" @click="toggleTable(t)">{{ t }}</span>
              </template>
              <span v-else class="hint-text" style="font-size:12px">该库暂无数据表，直接「备份数据库」即可</span>
            </div>
            <p class="form-tip">容器备份 = 整容器导出（tar）；目录备份 = 仅打包所选容器的网站目录（tar.gz，体积小）；
              数据库备份 = mysqldump（sql.gz）。文件保存在被控 /opt/ppanel/backups/。</p>
          </div>

          <!-- 定时备份任务 -->
          <div class="set-card" style="margin-top:14px">
            <div class="bar">
              <h3 class="set-title" style="margin:0">定时备份</h3>
              <span class="sp" />
              <UIButton type="ghost" size="sm" :loading="bjLoading" @click="loadBjJobs">刷新</UIButton>
              <UIButton size="sm" @click="bjOpen = !bjOpen">{{ bjOpen ? '收起' : '新建任务' }}</UIButton>
            </div>
            <div v-if="bjOpen" style="margin-top:10px">
              <div class="bk-line">
                <span class="bk-label">周期</span>
                <UIInput v-model="bjSchedule" style="width:190px" class="mono"
                         placeholder="cron：分 时 日 月 周" />
                <span v-for="p in BJ_PRESETS" :key="p.value" class="tbl-chip"
                      :class="{ on: bjSchedule === p.value }" @click="bjSchedule = p.value">{{ p.label }}</span>
              </div>
              <div class="bk-line">
                <span class="bk-label">类型</span>
                <UISelect v-model="bjKind" style="width:170px" :options="bjKindOpts" />
                <UISelect v-if="bjKind === 'container' || bjKind === 'dir'"
                          v-model="bjContainer" style="width:250px"
                          :options="bjContainerOpts" placeholder="选择容器" />
                <UISelect v-else v-model="bjDb" style="width:250px"
                          :options="bjDbOpts" placeholder="选择数据库" />
                <UIInput v-if="bjKind === 'dir'" v-model="bjDirPath" style="width:170px" class="mono"
                         placeholder="容器内目录，默认 /app" />
              </div>
              <div v-if="bjKind === 'db_table' && bjDb" class="bk-line" style="padding-left:70px">
                <template v-if="bjTableOpts.length">
                  <span class="hint-text" style="font-size:12px">选择单表（不选 = 整库）：</span>
                  <span v-for="t in bjTableOpts" :key="t" class="tbl-chip"
                        :class="{ on: bjTable === t }" @click="bjTable = bjTable === t ? '' : t">{{ t }}</span>
                </template>
                <span v-else class="hint-text" style="font-size:12px">该库暂无数据表，可改用整库备份</span>
              </div>
              <div class="bk-line">
                <span class="bk-label">保留</span>
                <UIInput v-model.number="bjKeep" style="width:80px" placeholder="5" />
                <span class="hint-text" style="font-size:12px">份（超出自动删最旧）</span>
                <UIButton size="sm" :loading="bjBusy" :disabled="!canSaveBj" @click="saveBjJob">创建并跑首份</UIButton>
              </div>
              <p class="form-tip">按周期自动备份，到点在被控后台执行；整库与表级任务互不干扰、各算各的保留份数。</p>
            </div>
            <table class="ftable" style="margin-top:12px">
              <thead><tr><th>周期</th><th>类型</th><th>目标</th><th>最近执行</th><th /></tr></thead>
              <tbody>
                <tr v-for="j in bjJobs" :key="j.id">
                  <td class="mono">{{ bjSchedLabel(j.schedule) }}</td>
                  <td><UITag tone="primary">{{ BJ_KIND_LABEL[j.kind] || j.kind }}</UITag></td>
                  <td class="mono">{{ bjTargetLabel(j) }}</td>
                  <td>
                    <UITag :tone="j.last_status === 'ok' ? 'ok' : j.last_status === 'fail' ? 'warn' : j.last_status === 'running' ? 'primary' : 'dim'">
                      {{ j.last_status === 'ok' ? '成功' : j.last_status === 'fail' ? '失败' : j.last_status === 'running' ? '执行中' : '未执行' }}
                    </UITag>
                    <span class="dim" style="font-size:12px;margin-left:6px">{{ fmtISO(j.last_run) }}</span>
                    <div v-if="j.last_output" class="hint-text" style="font-size:12px">{{ j.last_output.slice(0, 70) }}</div>
                  </td>
                  <td class="ops">
                    <UIButton type="text" @click="toggleBjJob(j)">{{ j.enabled ? '暂停' : '启用' }}</UIButton>
                    <UIButton type="text" @click="runBjJob(j)">立即执行</UIButton>
                    <UIButton type="text" class="danger" @click="delBjJob(j)">删除</UIButton>
                  </td>
                </tr>
                <tr v-if="!bjJobs.length"><td colspan="5" class="empty">暂无定时任务，点击「新建任务」创建</td></tr>
              </tbody>
            </table>
          </div>

          <!-- 恢复面板 -->
          <div v-if="bkRestore" class="set-card" style="margin-top:14px;border-color:var(--danger, #e5484d)">
            <template v-if="bkRestore.kind === 'container'">
              <div class="set-row" style="flex-wrap:wrap;gap:8px">
                <UIInput v-model="bkRestoreName" style="width:260px" placeholder="新容器名" />
                <UIButton size="sm" :loading="bkRestoreBusy" :disabled="!bkRestoreName.trim()"
                          @click="doRestore">恢复容器</UIButton>
                <UIButton type="ghost" size="sm" @click="bkRestore = null">取消</UIButton>
              </div>
              <p class="form-tip">将 <span class="mono">{{ bkRestore.file }}</span> 导入为镜像
                ppanel-restore:* 并创建新容器（sleep 入口，供进入容器取回数据；完整恢复服务请按原配置重建）。</p>
            </template>
            <template v-else-if="bkRestore.kind === 'dir'">
              <div class="set-row" style="flex-wrap:wrap;gap:8px">
                <UISelect v-model="bkRestoreCid" style="width:230px" :options="bkContainerOpts"
                          placeholder="目标容器" />
                <UIInput v-model="bkRestorePath" style="width:230px" class="mono"
                         placeholder="目标目录（如 /var/www/html）" />
                <UIButton size="sm" :loading="bkRestoreBusy"
                          :disabled="!bkRestoreCid || !bkRestorePath.trim()" @click="doRestore">恢复目录</UIButton>
                <UIButton type="ghost" size="sm" @click="bkRestore = null">取消</UIButton>
              </div>
              <p class="form-tip">将 {{ bkRestore.file }} 解压到目标容器的指定目录，覆盖同名文件（不删除目录下其他文件）。</p>
            </template>
            <template v-else>
              <div class="set-row" style="flex-wrap:wrap;gap:8px">
                <UISelect v-model="bkRestoreVer" style="width:200px" :options="bkVerOpts"
                          placeholder="目标 MySQL 版本" />
                <UIInput v-model="bkRestoreDb" style="width:220px" placeholder="目标库名" />
                <UIButton size="sm" :loading="bkRestoreBusy"
                          :disabled="!bkRestoreVer || !bkRestoreDb.trim()" @click="doRestore">恢复数据库</UIButton>
                <UIButton type="ghost" size="sm" @click="bkRestore = null">取消</UIButton>
              </div>
              <p class="form-tip" style="color:var(--danger, #e5484d)">
                将把 {{ bkRestore.file }} 覆盖导入到 MySQL {{ bkRestoreVer }} 的
                {{ bkRestoreDb.trim() || '—' }} 库，目标库现有数据会被覆盖，请谨慎操作！</p>
            </template>
          </div>

          <div class="bar" style="margin-top:14px">
            <span class="hint-text">共 {{ bkTotal }} 个备份文件</span>
            <span class="sp" />
            <UIButton type="ghost" size="sm" :loading="backupsLoading" @click="loadBackups">刷新</UIButton>
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
                  <UIButton v-if="b.kind !== 'other'" type="text" @click="startRestore(b)">恢复</UIButton>
                  <UIButton type="text" @click="downloadBackup(b.file)">下载</UIButton>
                  <UIButton type="text" class="danger" @click="askDelBackup(b.file)">删除</UIButton>
                </td>
              </tr>
              <tr v-if="!backups.length"><td colspan="5" class="empty">暂无备份，可从上方创建</td></tr>
            </tbody>
          </table>
          <UIPager v-if="bkTotal > 0" class="bk-pager"
                   :total="bkTotal" v-model:page="bkPage" v-model:pageSize="bkPageSize"
                   @change="loadBackups" />
        </div>

        <!-- 设置：磁盘占用 / 一键清理 / 镜像加速 -->
        <div v-else-if="view === 'settings'">
          <h3 class="set-title">磁盘占用</h3>
          <div v-if="df" class="ov-grid">
            <div v-for="[k, v] in dfItems" :key="k" class="ov-cell">
              <span class="k">{{ k }}</span>
              <span class="v mono">{{ v.size_text }}</span>
              <span class="k">{{ v.count }} 项</span>
            </div>
          </div>

          <h3 class="set-title">一键清理</h3>
          <div class="set-card">
            <p class="form-tip">清理<b>已停止的容器</b>、<b>悬空镜像</b>和<b>无用网络</b>，不会触碰数据卷与运行中的容器。</p>
            <div class="set-row">
              <label class="check-line">
                <input v-model="pruneBuilder" type="checkbox">
                <span>同时清理构建缓存</span>
              </label>
              <UIButton type="danger" :loading="pruneRunning" @click="doSystemPrune">开始清理</UIButton>
            </div>
          </div>

          <h3 class="set-title">镜像加速</h3>
          <div class="set-card">
            <p class="form-tip">配置 Docker Registry 镜像源（daemon.json 的 registry-mirrors），每行一个地址。保存后 Docker 将自动重启（约数秒），运行中的容器不受影响。</p>
            <textarea v-model="mirrorsText" class="ta mono" rows="4"
                      placeholder="https://docker.m.daocloud.io&#10;https://dockerproxy.com" />
            <div class="set-row">
              <UIButton :loading="savingMirrors" @click="saveMirrors">保存并重启 Docker</UIButton>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- 输入弹窗（新建/重命名） -->
    <UIModal v-model:open="promptOpen" :title="promptTitle" width="380px">
      <label class="p-field">
        <span>{{ promptLabel }}</span>
        <UIInput v-model="promptValue" @keyup.enter="confirmPrompt" />
      </label>
      <template #footer>
        <UIButton type="ghost" @click="promptOpen = false">取消</UIButton>
        <UIButton @click="confirmPrompt">确定</UIButton>
      </template>
    </UIModal>

    <!-- Monaco 编辑弹窗 -->
    <UIModal v-model:open="editOpen" width="900px" :title="`编辑 - ${editPath.split('/').pop()}`">
      <div class="edit-host-wrap">
        <CodeEditor v-model="editContent" :filename="editPath" @save="saveEdit" />
      </div>
      <template #footer>
        <span class="edit-lang mono">{{ editLangName }}<i v-if="editDirty" class="dirty">•</i></span>
        <span class="sp" />
        <UIButton type="ghost" @click="editOpen = false">取消</UIButton>
        <UIButton :loading="editSaving" @click="saveEdit">保存</UIButton>
      </template>
    </UIModal>

    <!-- 压缩弹窗 -->
    <UIModal v-model:open="zipOpen" title="压缩" width="420px">
      <div class="p-field">
        <span>压缩格式</span>
        <UISelect v-model="zipType" :options="[{ value: 'tar.gz', label: 'tar.gz' }, { value: 'zip', label: 'zip' }]" />
      </div>
      <label class="p-field">
        <span>压缩包名（保存到当前目录）</span>
        <UIInput v-model="zipName" />
      </label>
      <p class="form-tip">将压缩 {{ zipTargets.length }} 项到当前目录</p>
      <template #footer>
        <UIButton type="ghost" @click="zipOpen = false">取消</UIButton>
        <UIButton @click="submitZip">压缩</UIButton>
      </template>
    </UIModal>

    <!-- 图片预览 -->
    <UIModal v-model:open="previewOpen" width="640px" :title="`图片预览 - ${previewName}`">
      <div class="preview-wrap">
        <img v-if="previewUrl" :src="previewUrl" alt="preview">
        <span v-else class="dim">加载中…</span>
      </div>
    </UIModal>

    <!-- 上传弹窗 -->
    <UIModal v-model:open="upOpen" width="640px" :title="`上传文件到：${curPath}`">
      <div class="up-toolbar">
        <UIButton type="ghost" @click="pickUpFiles">选择文件</UIButton>
        <span class="hint-text">{{ upItems.length ? `共 ${upItems.length} 个 · 待传 ${pendingCount()} 个` : '' }}</span>
        <span class="sp" />
        <UIButton :disabled="upUploading || !pendingCount()" :loading="upUploading" @click="startUpload">
          {{ upUploading ? '上传中...' : '开始上传' }}
        </UIButton>
      </div>
      <div v-if="upItems.length" class="up-table-wrap">
        <table class="ftable up-table">
          <tbody>
            <tr v-for="item in upItems" :key="item.id">
              <td class="up-name" :title="item.name">
                <span class="up-icon" :class="item.status">{{ item.status === 'success' ? '✓' : item.status === 'error' ? '!' : '↑' }}</span>
                <span class="up-name-text">{{ item.name }}</span>
                <span v-if="item.resumed" class="resume-tag">续传</span>
              </td>
              <td class="mono dim up-size">{{ fmtSize(item.size) }}</td>
              <td>
                <div class="up-progress-row">
                  <div class="up-bar"><div class="up-bar-fill" :class="{ err: item.status === 'error' }" :style="{ width: item.progress + '%' }" /></div>
                  <span class="up-percent mono">{{ item.status === 'success' ? '完成' : item.status === 'error' ? '失败' : Math.floor(item.progress) + '%' }}</span>
                </div>
              </td>
              <td class="up-act">
                <button v-if="item.status === 'error'" class="link" @click="upOne(item)">重传</button>
                <button v-else-if="item.status !== 'uploading'" class="link danger" @click="removeUpItem(item)">移除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="up-empty">尚未选择文件</div>
    </UIModal>

    <!-- 解压 / 删除确认 -->
    <ConfirmDialog v-model:open="unzipOpen" title="解压"
                   :message="`确定解压「${unzipName}」到当前目录吗？`"
                   confirm-text="解压" @confirm="confirmUnzip" />
    <ConfirmDialog v-model:open="delOpen" title="删除确认"
                   :message="`确定删除 ${delList.length > 1 ? delList.length + ' 项' : '「' + delList[0] + '」'} 吗？此操作不可恢复。`"
                   confirm-text="删除" danger @confirm="confirmDel" />

    <!-- 容器删除确认 -->
    <ConfirmDialog v-model:open="contDelOpen" title="删除容器"
                   :message="contDelTarget?.state === 'running'
                     ? `容器「${contDelTarget?.name}」正在运行，将强制删除。确定继续吗？`
                     : `确定删除容器「${contDelTarget?.name}」吗？`"
                   confirm-text="删除" danger @confirm="confirmDelContainer" />

    <!-- 镜像删除确认 -->
    <ConfirmDialog v-model:open="imgDelOpen" title="删除镜像"
                   :message="imgDelForce
                     ? `镜像「${imgDelTarget?.full_name}」正被容器使用，确定强制删除吗？`
                     : `确定删除镜像「${imgDelTarget?.full_name}」吗？`"
                   confirm-text="删除" danger @confirm="confirmDelImage" />

    <!-- 容器日志 -->
    <UIModal v-model:open="logOpen" width="820px" :title="`容器日志 - ${logName}`">
      <pre class="log-view mono">{{ logText }}</pre>
      <template #footer>
        <UIButton type="ghost" @click="logOpen = false">关闭</UIButton>
      </template>
    </UIModal>

    <!-- 拉取镜像 -->
    <UIModal v-model:open="pullOpen" title="拉取镜像" width="440px">
      <label class="p-field">
        <span>镜像名（含标签，如 python:3.11-slim）</span>
        <UIInput v-model="pullImage" placeholder="python:3.11-slim" @keyup.enter="doPull" />
      </label>
      <p class="form-tip">拉取过程可能需要数十秒，请耐心等待。</p>
      <template #footer>
        <UIButton type="ghost" :disabled="pullLoading" @click="pullOpen = false">取消</UIButton>
        <UIButton :loading="pullLoading" @click="doPull">拉取</UIButton>
      </template>
    </UIModal>

    <!-- 构建 PHP 增强镜像 -->
    <UIModal v-model:open="phpBuildOpen" title="构建 PHP 增强镜像" width="680px">
      <template v-if="!phpBuildLines.length">
        <label class="p-field">
          <span>基础镜像（需已拉取的官方 php:*-cli）</span>
          <UISelect v-model="phpBuildBase" :options="phpBuildBaseOptions.map(o => ({ value: o, label: o }))" />
        </label>
        <p class="form-tip">
          在官方镜像上编译常用扩展：mysqli、pdo_mysql、gd、zip、bcmath、intl、sockets、exif、opcache，
          产物 ppanel-php:8.x-full 自动加入该节点运行环境白名单（用户切换列表可见）。
          构建约需 2-10 分钟，树莓派等 ARM 节点更久。
        </p>
      </template>
      <pre v-else class="build-log mono">{{ phpBuildLines.join('\n') }}</pre>
      <template #footer>
        <UIButton type="ghost" @click="phpBuildOpen = false">关闭（构建后台继续）</UIButton>
        <UIButton v-if="!phpBuildLines.length" :loading="phpBuildBusy" @click="doPhpBuild">开始构建</UIButton>
        <UIButton v-else-if="phpBuildDone" @click="phpBuildOpen = false">完成</UIButton>
      </template>
    </UIModal>

    <!-- 防火墙：开放/拒绝端口 -->
    <UIModal v-model:open="fwPortOpen" title="端口规则" width="420px">
      <div class="p-field">
        <span>动作</span>
        <UISelect v-model="fwAction" :options="[{ value: 'allow', label: '允许（放行）' }, { value: 'deny', label: '拒绝（封禁）' }]" />
      </div>
      <div class="p-field">
        <span>端口 / 端口段</span>
        <UIInput v-model="fwPort" placeholder="8080 或 30000:39999" @keyup.enter="saveFwRule" />
      </div>
      <div class="p-field">
        <span>协议</span>
        <UISelect v-model="fwProto" :options="[{ value: 'tcp', label: 'TCP' }, { value: 'udp', label: 'UDP' }]" />
      </div>
      <template #footer>
        <UIButton type="ghost" @click="fwPortOpen = false">取消</UIButton>
        <UIButton :loading="fwSaving" @click="saveFwRule">确定</UIButton>
      </template>
    </UIModal>

    <!-- 防火墙：启停确认 -->
    <ConfirmDialog v-model:open="fwToggleOpen" title="切换防火墙状态"
                   :message="fw?.active
                     ? '停用防火墙后所有端口将对公网开放（仅系统自身防护），确定停用吗？'
                     : '启用防火墙前请确认已放行节点端口（9100 及 SSH 22），否则可能失联。确定启用吗？'"
                   confirm-text="确定" danger @confirm="doFwToggle" />

    <!-- SSH 防护：启停确认 -->
    <ConfirmDialog v-model:open="secToggleOpen" title="切换 fail2ban"
                   :message="sec?.active
                     ? '停用后节点将不再自动封禁 SSH 爆破 IP，确定停用吗？'
                     : '启用 fail2ban 防爆破（失败 5 次封 10 分钟）。确定启用吗？'"
                   confirm-text="确定" :danger="sec?.active" @confirm="doSecToggle" />

    <!-- SSH 防护：解封确认 -->
    <ConfirmDialog v-model:open="secUnbanOpen" :title="`解封 ${secUnbanIp}`"
                   message="解封后该 IP 可立即重新连接 SSH。确定解封吗？"
                   confirm-text="解封" @confirm="doSecUnban" />

    <!-- MySQL 停用确认（可选同时删数据卷） -->
    <UIModal v-model:open="mysqlDisableOpen" title="停用 MySQL 服务" width="440px">
      <p class="form-tip">将停止并删除 MySQL {{ mysqlDisableVer }} 容器，该版本下所有实例数据库将无法连接。</p>
      <label class="check-line">
        <input v-model="mysqlPurgeVol" type="checkbox">
        <span>同时删除数据卷（所有库数据不可恢复）</span>
      </label>
      <template #footer>
        <UIButton type="ghost" @click="mysqlDisableOpen = false">取消</UIButton>
        <UIButton type="danger" @click="confirmDisableMysql">确认停用</UIButton>
      </template>
    </UIModal>

    <!-- MySQL root 密码查看 -->
    <UIModal v-model:open="mysqlRootOpen" title="MySQL root 凭据" width="440px">
      <label class="p-field">
        <span>root 密码（请勿泄露；重置 MySQL 服务需停用后重新启用）</span>
        <UIInput :model-value="mysqlRootInfo?.root_password || ''" readonly />
      </label>
      <p class="form-tip">外网端口：{{ mysqlRootInfo?.host_port || '-' }}（所有库共用，按账号隔离）</p>
      <template #footer>
        <UIButton type="ghost" @click="mysqlRootOpen = false">关闭</UIButton>
      </template>
    </UIModal>

    <!-- 备份删除确认 -->
    <ConfirmDialog v-model:open="bkDelOpen" title="删除备份"
                   :message="`确定删除备份文件「${bkDelFile}」吗？此操作不可恢复。`"
                   confirm-text="删除" danger @confirm="doDelBackup" />

    <!-- 右键菜单 -->
    <teleport to="body">
      <div v-if="ctxOpen" class="nctx-menu" :style="{ left: ctxX + 'px', top: ctxY + 'px' }">
        <template v-for="(it, i) in ctxItems" :key="i">
          <div v-if="it.sep" class="ctx-sep" />
          <button v-else class="ctx-item" :class="{ danger: it.danger, disabled: it.disabled }"
                  :disabled="it.disabled" @click="closeCtx(); it.fn()">
            <span class="ctx-icon" v-html="biSvg(it.icon)" />
            <span class="ctx-label">{{ it.label }}</span>
          </button>
        </template>
      </div>
    </teleport>
  </UIModal>
</template>

<style scoped>
.build-log {
  height: 320px; overflow: auto; margin: 0;
  background: #0c0c0f; color: #c9d1d9; border-radius: 8px;
  padding: 12px 14px; font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
}
.nam-body { display: flex; height: 82vh; }
.nam-side { width: 148px; border-right: 1px solid var(--line); padding: 10px 8px; display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  position: relative; display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px; border: none; border-radius: 8px; background: transparent;
  font-size: 13px; font-family: inherit; color: var(--text-2); cursor: pointer; transition: all .15s;
}
.nav-item:hover { background: color-mix(in srgb, var(--primary) 8%, transparent); color: var(--text-1); }
.nav-item.active { background: color-mix(in srgb, var(--primary) 12%, transparent); color: var(--primary); font-weight: 600; }
.nav-item .ind { width: 4px; height: 14px; border-radius: 2px; background: var(--primary); }

.nam-main { flex: 1; overflow: auto; padding: 16px 18px; }

.ov-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.ov-cell { border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; display: flex; flex-direction: column; gap: 4px; }
.ov-cell .k { font-size: 12px; color: var(--text-dim); }
.ov-cell .v { font-size: 14px; color: var(--text-1); }

/* 稳定性评分卡 */
.stab-card { margin-top: 18px; border: 1px solid var(--line); border-radius: var(--radius); padding: 14px 16px; }
.stab-card .bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.win-chips { display: flex; gap: 6px; }
.win-chip { border: 1px solid var(--line); background: transparent; color: var(--text-dim); border-radius: 999px; padding: 3px 12px; font-size: 12px; cursor: pointer; transition: all .2s; }
.win-chip.active { border-color: var(--brand, #d4b878); color: var(--text-1); background: rgba(212, 184, 120, .12); }
.stab-head { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.stab-score { font-size: 34px; font-weight: 700; line-height: 1; }
.st-excellent, .st-good { color: #30a46c; }
.st-fair { color: #d9a03f; }
.st-poor { color: #e5484d; }
.st-observing { color: var(--text-dim); }
.stab-meta { display: flex; flex-direction: column; gap: 4px; }
.stab-meta .dim { font-size: 12px; color: var(--text-dim); }
.stab-parts { display: flex; gap: 18px; margin-left: auto; }
.sp { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.sp i { font-style: normal; font-size: 11px; color: var(--text-dim); }
.sp b { font-size: 15px; font-weight: 600; color: var(--text-1); }
.stab-facts { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--line); font-size: 12px; }

/* 仪表盘圆环 */
.dash-row { display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 18px; }
.gauge-card { display: flex; flex-direction: column; align-items: center; gap: 8px; }
.gauge-wrap { position: relative; width: 132px; height: 132px; }
.gauge-wrap svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.g-bg { fill: none; stroke: var(--line); stroke-width: 10; }
.g-val { fill: none; stroke-width: 10; stroke-linecap: round; transition: stroke-dashoffset .6s ease, stroke .3s; }
.g-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; }
.g-num { font-size: 20px; font-weight: 700; color: var(--text-1); }
.g-label { font-size: 12px; color: var(--text-dim); }
.g-sub { max-width: 150px; font-size: 11px; color: var(--text-dim); text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.hint-text { font-size: 12px; color: var(--text-dim); margin: 0; }

table.ftable { width: 100%; border-collapse: collapse; font-size: 13px; }
.ftable th { text-align: left; padding: 8px 10px; font-size: 12px; color: var(--text-dim); border-bottom: 1px solid var(--line); font-weight: 500; }
.ftable td { padding: 7px 10px; border-bottom: 1px solid color-mix(in srgb, var(--line) 55%, transparent); vertical-align: middle; }
.ftable tbody tr { cursor: default; }
.ftable tbody tr:hover td { background: color-mix(in srgb, var(--primary) 4%, transparent); }
.ftable tbody tr.selected td { background: color-mix(in srgb, var(--primary) 10%, transparent); }
.empty { text-align: center; color: var(--text-dim); padding: 26px 0 !important; }
.bk-pager { justify-content: flex-end; margin-top: 10px; }
.ops { white-space: nowrap; text-align: right; }
.dim { color: var(--text-dim); }
.link { border: none; background: none; font-family: inherit; font-size: 12.5px; color: var(--primary); cursor: pointer; padding: 0 4px; }
.link.danger { color: #e5484d; }

/* ---------- 文件管理（独立面板同款） ---------- */
.fm { user-select: none; }
.fm-bar { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
.tool {
  height: 26px; padding: 0 11px; font-size: 12px; font-family: inherit;
  border: 1px solid var(--line); border-radius: 6px; background: var(--card-bg);
  color: var(--text-2); cursor: pointer; transition: all .15s;
}
.tool:hover { border-color: var(--primary); color: var(--primary); }
.crumbs { flex: 1; min-width: 160px; overflow-x: auto; white-space: nowrap; padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; font-size: 12px; }
.crumb { border: none; background: none; color: var(--text-2); cursor: pointer; font-family: inherit; font-size: 12px; padding: 0; }
.crumb:hover { color: var(--primary); }
.sep { color: var(--text-dim); margin: 0 2px; }
.clip-tip { font-size: 12px; color: var(--primary); white-space: nowrap; }

.sel-bar {
  display: flex; align-items: center; gap: 10px; padding: 7px 12px; margin-bottom: 10px;
  border-radius: 10px; background: color-mix(in srgb, var(--primary) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--primary) 28%, transparent);
}
.sel-bar .sel-count { font-size: 13px; white-space: nowrap; color: var(--text-2); }
.sel-bar .sel-count b { color: var(--primary); font-size: 15px; }
.sel-actions { display: flex; gap: 6px; flex: 1; }
.sbtn {
  height: 24px; padding: 0 12px; font-size: 12px; font-family: inherit; cursor: pointer;
  border: 1px solid var(--line); border-radius: 6px; background: var(--card-bg); color: var(--text-2);
  transition: all .15s;
}
.sbtn:hover { border-color: var(--primary); color: var(--primary); }
.sbtn.danger { color: #e5484d; }
.sbtn.danger:hover { border-color: #e5484d; }
.sel-cancel { border: none; background: transparent; color: var(--text-dim); font-size: 12px; cursor: pointer; padding: 4px 6px; border-radius: 6px; font-family: inherit; }
.sel-cancel:hover { color: var(--text-1); }

.name-cell { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.f-svg { width: 1em; height: 1em; font-size: 20px; vertical-align: -0.15em; fill: currentColor; overflow: hidden; flex-shrink: 0; color: var(--primary); }
tr[data-kind='file'] .f-svg { color: var(--text-2); }
.fname-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px; }
.size-link { color: var(--primary); cursor: pointer; font-size: 12px; }
.size-link:hover { text-decoration: underline; }
.table-actions { display: inline-flex; gap: 2px; }
.tbtn {
  border: none; background: transparent; cursor: pointer; padding: 5px; border-radius: 6px;
  color: var(--text-dim); display: inline-flex; transition: all .15s;
}
.tbtn:hover { color: var(--primary); background: color-mix(in srgb, var(--primary) 10%, transparent); }
.tbtn.danger:hover { color: #e5484d; background: rgba(224, 82, 96, .1); }
.tbtn :deep(svg) { width: 14px; height: 14px; }

/* 复选框（独立面板 .ck 同款） */
.ck {
  width: 15px; height: 15px; border-radius: 4px; border: 1.5px solid #c9d2cd; background: #fff;
  display: inline-flex; align-items: center; justify-content: center; cursor: pointer;
  transition: all .15s; vertical-align: middle;
}
.ck:hover { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(47, 181, 159, .12); }
.ck :deep(svg) { width: 9px; height: 9px; color: #fff; opacity: 0; transform: scale(.4); transition: all .15s; }
.ck.on { background: var(--primary); border-color: var(--primary); }
.ck.on :deep(svg) { opacity: 1; transform: scale(1); }
.ck.ind { background: color-mix(in srgb, var(--primary) 14%, transparent); border-color: var(--primary); }
.ck-ind { width: 7px; height: 2px; border-radius: 1px; background: var(--primary); }

.fscroll { overflow: auto; }

/* 编辑弹窗 */
.edit-host-wrap { height: 62vh; display: flex; }
.edit-host-wrap > * { flex: 1; min-width: 0; }
.edit-lang { font-size: 12px; color: var(--text-dim); }
.edit-lang .dirty { color: var(--primary); margin-left: 4px; font-style: normal; }
.sp { flex: 1; }

.p-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.p-field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
.form-tip { font-size: 12px; color: var(--text-dim); margin: 0; }

/* 图片预览 */
.preview-wrap { display: flex; align-items: center; justify-content: center; min-height: 200px; }
.preview-wrap img { max-width: 100%; max-height: 56vh; border-radius: 8px; }

/* 上传弹窗 */
.up-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.up-table-wrap { max-height: 46vh; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
.up-table td { padding: 8px 10px; }
.up-name { display: flex; align-items: center; gap: 8px; max-width: 240px; }
.up-name-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12.5px; }
.up-icon {
  width: 18px; height: 18px; border-radius: 50%; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700; color: #fff; background: var(--text-dim);
}
.up-icon.success { background: #10b981; }
.up-icon.error { background: #e5484d; }
.resume-tag {
  font-size: 10px; padding: 1px 6px; border-radius: 4px; flex-shrink: 0;
  background: color-mix(in srgb, var(--primary) 14%, transparent); color: var(--primary);
}
.up-size { width: 80px; white-space: nowrap; }
.up-progress-row { display: flex; align-items: center; gap: 8px; }
.up-bar { flex: 1; height: 6px; border-radius: 3px; background: color-mix(in srgb, var(--line) 60%, transparent); overflow: hidden; min-width: 80px; }
.up-bar-fill { height: 100%; background: var(--primary); transition: width .2s; }
.up-bar-fill.err { background: #e5484d; }
.up-percent { width: 46px; text-align: right; white-space: nowrap; }
.up-act { width: 60px; text-align: right; }
.up-empty {
  min-height: 120px; display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 13px; border: 1px dashed var(--line); border-radius: 8px;
}

/* 右键菜单（独立面板同款） */
.nctx-menu {
  position: fixed; z-index: 2101; min-width: 150px; padding: 5px;
  background: var(--card-bg); border: 1px solid var(--line); border-radius: 10px;
  box-shadow: 0 12px 32px rgba(22, 25, 29, .16); display: flex; flex-direction: column;
}
.ctx-item {
  border: none; background: none; text-align: left; font-family: inherit;
  font-size: 12.5px; color: var(--text-2); padding: 7px 12px; border-radius: 6px;
  cursor: pointer; display: inline-flex; align-items: center; gap: 9px;
}
.ctx-item:hover { background: color-mix(in srgb, var(--primary) 10%, transparent); color: var(--text-1); }
.ctx-item.danger { color: #e5484d; }
.ctx-item.disabled { opacity: .45; cursor: not-allowed; }
.ctx-item :deep(svg) { width: 13px; height: 13px; flex-shrink: 0; }
.ctx-sep { height: 1px; background: var(--line); margin: 4px 8px; }

.term-slot { height: 100%; }

/* 设置视图 */
.set-title { font-size: 13px; font-weight: 600; color: var(--text-1); margin: 20px 0 10px; }
.set-title:first-child { margin-top: 0; }
.set-card { border: 1px solid var(--line); border-radius: var(--radius); padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }

.tbl-chip {
  display: inline-block; padding: 3px 10px; border-radius: 999px; cursor: pointer;
  font-size: 12px; font-family: var(--font-mono);
  background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.14);
  color: var(--text-dim, #9aa); transition: all .15s ease; user-select: none;
}
.tbl-chip:hover { border-color: rgba(47,181,159,.5); color: #fff; }
.tbl-chip.on { background: rgba(47,181,159,.18); border-color: var(--primary); color: var(--primary); }
.bk-line { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.bk-label { width: 60px; flex-shrink: 0; font-size: 13px; color: var(--text-2); }
.set-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.check-line { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-2); cursor: pointer; }
.check-line input { accent-color: var(--primary); width: 14px; height: 14px; cursor: pointer; }
.ta {
  width: 100%; resize: vertical; border: 1px solid var(--line); border-radius: 8px;
  background: var(--card-bg); color: var(--text-1); padding: 9px 11px;
  font-size: 12.5px; line-height: 1.6; font-family: inherit; outline: none;
}
.ta:focus { border-color: var(--primary); }

/* 容器日志 */
.log-view {
  margin: 0; height: 56vh; overflow: auto; white-space: pre-wrap; word-break: break-all;
  background: #10161a; color: #d5e0dc; border-radius: 8px; padding: 12px 14px; font-size: 12px; line-height: 1.55;
}

/* 防火墙 */
.fw-head { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.fw-tool { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; }
.fw-tool em { font-style: normal; font-weight: 400; color: var(--text-3); font-size: 12.5px; }
.fw-dot { width: 8px; height: 8px; border-radius: 50%; }
.fw-dot.on { background: #2fb59f; box-shadow: 0 0 6px rgba(47, 181, 159, .55); }
.fw-dot.off { background: #c96a5a; }
.fw-none { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.fw-none code { color: var(--primary-strong); font-size: 12px; }
.danger-tip { color: #d98a7c; }
.fw-badge { font-style: normal; font-size: 11.5px; font-weight: 600; padding: 2px 8px; border-radius: 10px; }
.fw-badge.allow { color: #2fb59f; background: rgba(47, 181, 159, .12); }
.fw-badge.deny { color: #d98a7c; background: rgba(217, 138, 124, .14); }

/* SSH 防护 */
.sec-stats { display: flex; gap: 12px; margin-top: 12px; }
.sec-stat { flex: 1; background: var(--panel-2, rgba(255, 255, 255, .04)); border: 1px solid var(--line, rgba(255, 255, 255, .08)); border-radius: 12px; padding: 14px 10px; text-align: center; }
.sec-stat b { display: block; font-size: 22px; line-height: 1.3; }
.sec-stat span { font-size: 12px; color: var(--text-3); }
.sec-stat b.hot { color: #d98a7c; }
.sec-bans { display: flex; flex-wrap: wrap; gap: 8px; }
.sec-ban-ip { display: inline-flex; align-items: center; gap: 8px; color: #d98a7c; background: rgba(217, 138, 124, .1); border: 1px solid rgba(217, 138, 124, .28); border-radius: 9px; padding: 4px 6px 4px 10px; font-size: 13px; }
.sec-ban-ip i { cursor: pointer; font-style: normal; font-weight: 700; width: 18px; height: 18px; line-height: 17px; text-align: center; border-radius: 50%; }
.sec-ban-ip i:hover { background: rgba(217, 138, 124, .25); }
</style>
