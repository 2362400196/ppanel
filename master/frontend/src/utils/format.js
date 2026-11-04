export function fmtBytes(n) {
  if (n == null) return '-'
  if (n < 1024) return n + ' B'
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n
  let i = -1
  do { v /= 1024; i++ } while (v >= 1024 && i < units.length - 1)
  return v.toFixed(v >= 100 ? 0 : 1) + ' ' + units[i]
}

// 复制文本到剪贴板：优先 Clipboard API（仅 HTTPS/localhost 可用），
// 非 HTTPS 环境（如 http://IP:端口）降级为隐藏 textarea + execCommand。
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true } catch { /* 继续降级 */ }
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.cssText = 'position:fixed;top:-100px;left:-100px;opacity:0'
    document.body.appendChild(ta)
    const sel = document.getSelection()
    const saved = sel && sel.rangeCount ? sel.getRangeAt(0) : null
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    ta.remove()
    if (saved && sel) { sel.removeAllRanges(); sel.addRange(saved) }
    return ok
  } catch {
    return false
  }
}

export function fmtTime(s) {
  if (!s) return '-'
  const d = new Date(s)
  if (isNaN(d.getTime())) return String(s)
  const p = x => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export const STATUS_TEXT = {
  creating: '创建中',
  created: '已创建',
  running: '运行中',
  restarting: '崩溃重启中',
  exited: '已停止'
}

export const STATUS_TONE = {
  creating: 'warn',
  created: 'dim',
  running: 'ok',
  restarting: 'bad',
  exited: 'dim'
}
