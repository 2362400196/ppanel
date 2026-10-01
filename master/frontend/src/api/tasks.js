/** 全局任务终端状态：耗时操作实时日志（X-Task-Id 约定，见后端 app/tasks.py）。
 *
 * 用法：
 *   import { openTask } from '../api/tasks'
 *   const tid = crypto.randomUUID()
 *   openTask(tid, { node: '1' })          // 先开终端，日志实时滚出
 *   await api.post('/host/backups/container', data,
 *                  { params: np(), headers: { 'X-Task-Id': tid } })
 */
import { api } from './client'
import { reactive } from 'vue'

export const taskState = reactive({
  open: false,
  taskId: '',
  node: '',       // 空 = 主控任务；数字 = 节点 id（经 /api/host 转发拉被控日志）
  lines: [],
  status: '',
  _timer: null,
})

let _polling = false

export function openTask(taskId, { node = '' } = {}) {
  taskState.taskId = taskId
  taskState.node = String(node || '')
  taskState.lines = []
  taskState.status = 'running'
  taskState.open = true
  pollTask()
  if (!taskState._timer) taskState._timer = setInterval(pollTask, 1500)
}

export function closeTask() {
  taskState.open = false
  if (taskState._timer) { clearInterval(taskState._timer); taskState._timer = null }
}

async function pollTask() {
  if (!taskState.open || !taskState.taskId || _polling) return
  _polling = true
  try {
    const since = taskState.lines.length
    const { data } = taskState.node
      ? await api.get('/host/tasks/lines', { params: {
          task_id: taskState.taskId, node: taskState.node, since } })
      : await api.get('/tasks/lines', { params: { task_id: taskState.taskId, since } })
    if (data.lines?.length) taskState.lines.push(...data.lines)
    if (data.status && data.status !== 'running') {
      taskState.status = data.status
      if (taskState._timer) { clearInterval(taskState._timer); taskState._timer = null }
    }
  } catch { /* 终端日志拉取失败静默，不影响业务请求 */ }
  finally { _polling = false }
}

/** 生成任务 id：crypto.randomUUID 仅安全上下文（HTTPS/localhost）可用，
 *  通过 http://IP:端口 访问时为 undefined，用 getRandomValues 兜底生成 v4 UUID。 */
function genTaskId() {
  if (crypto.randomUUID) return crypto.randomUUID()
  return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, c =>
    (+c ^ crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (+c / 4))).toString(16))
}

/** 生成任务 id 并打开终端；返回应随请求携带的 X-Task-Id。 */
export function startTask(node = '') {
  const tid = genTaskId()
  openTask(tid, { node })
  return tid
}
