export function fmtBytes(n) {
  if (n == null) return '-'
  if (n < 1024) return n + ' B'
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n
  let i = -1
  do { v /= 1024; i++ } while (v >= 1024 && i < units.length - 1)
  return v.toFixed(v >= 100 ? 0 : 1) + ' ' + units[i]
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
  exited: '已停止'
}

export const STATUS_TONE = {
  creating: 'warn',
  created: 'dim',
  running: 'ok',
  exited: 'dim'
}
