<script setup>
// 工单会话（用户/管理员共用）：气泡消息（Markdown 代码渲染）+ 图片/文件附件 + 回复 + 关闭
import { nextTick, onUnmounted, ref, watch } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from './ui/toast'
import UIButton from './ui/UIButton.vue'
import UITag from './ui/UITag.vue'
import UIModal from './ui/UIModal.vue'
import { fmtTime } from '../utils/format'

marked.setOptions({ breaks: true, gfm: true })

const props = defineProps({
  ticketId: { type: Number, default: 0 },
  adminMode: Boolean,          // 管理员视角：右侧为用户、左侧为自己回复
})
const open = defineModel('open', { type: Boolean, default: false })
const emit = defineEmits(['changed'])   // 回复/上传/关闭后通知父级刷新列表

const info = ref(null)        // 工单头（标题/状态/用户名）
const msgs = ref([])
const draft = ref('')
const sending = ref(false)
const bodyEl = ref(null)
const fileEl = ref(null)      // 隐藏 input
const uploading = ref(false)
const imgUrl = ref({})        // file_id -> objectURL（图片预览）
let timer = null

// ``` 围栏 → 行内代码块 → 段落；富文本经 DOMPurify 消毒
function renderMd(text) {
  const html = marked.parse(text || '')
  return DOMPurify.sanitize(html, { FORBID_TAGS: ['style'], FORBID_ATTR: ['style'] })
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1048576).toFixed(1)} MB`
}

async function ensureImg(m) {
  if (!m.file?.is_image || imgUrl.value[m.file.id]) return
  try {
    const { data } = await api.get(`/tickets/files/${m.file.id}`, { responseType: 'blob' })
    imgUrl.value[m.file.id] = URL.createObjectURL(data)
  } catch { /* 预览失败不影响消息展示 */ }
}

async function load() {
  if (!props.ticketId) return
  try {
    const { data } = await api.get(`/tickets/${props.ticketId}`)
    info.value = data
    msgs.value = data.messages
    data.messages.forEach(m => ensureImg(m))
    await nextTick()
    if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight
  } catch (e) {
    toastErr(errText(e))
  }
}

watch(open, v => {
  clearInterval(timer)
  if (v) { load(); timer = setInterval(load, 4000) }
  else {
    info.value = null; msgs.value = []; draft.value = ''
    Object.values(imgUrl.value).forEach(u => URL.revokeObjectURL(u))
    imgUrl.value = {}
  }
})
onUnmounted(() => { clearInterval(timer); Object.values(imgUrl.value).forEach(u => URL.revokeObjectURL(u)) })

defineExpose({ load })   // 父组件在回复/关闭后可刷新列表

async function send() {
  const content = draft.value.trim()
  if (!content || sending.value) return
  sending.value = true
  try {
    await api.post(`/tickets/${props.ticketId}/reply`, { content })
    draft.value = ''
    emit('changed')
    await load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    sending.value = false
  }
}

// 附件：选择后立即上传为一条消息（图片 ≤10MB，白名单类型）
async function onPick(e) {
  const f = e.target.files?.[0]
  e.target.value = ''
  if (!f || uploading.value) return
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', f)
    await api.post(`/tickets/${props.ticketId}/files`, fd)
    toastOk('附件已发送')
    emit('changed')
    await load()
  } catch (err) {
    toastErr(errText(err))
  } finally {
    uploading.value = false
  }
}

function onPaste(e) {
  // 支持直接 Ctrl+V 粘贴截图/文件
  const f = e.clipboardData?.files?.[0]
  if (f && fileEl.value) {
    e.preventDefault()
    const dt = new DataTransfer()
    dt.items.add(f)
    fileEl.value.files = dt.files
    onPick({ target: fileEl.value })
  }
}

const closing = ref(false)
async function closeTicket() {
  if (closing.value) return
  closing.value = true
  try {
    await api.post(`/tickets/${props.ticketId}/close`)
    toastOk('工单已关闭')
    emit('changed')
    await load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    closing.value = false
  }
}
</script>

<template>
  <UIModal v-model:open="open" :title="`工单 #${ticketId} · ${info?.title || ''}`" width="600px">
    <div v-if="info" class="t-head">
      <UITag :tone="info.status === 'open' ? 'ok' : 'info'">{{ info.status === 'open' ? '处理中' : '已关闭' }}</UITag>
      <span v-if="adminMode" class="t-user">来自用户 {{ info.username }}</span>
      <UIButton v-if="info.status === 'open'" type="text" class="danger" :disabled="closing" @click="closeTicket">关闭工单</UIButton>
    </div>

    <div ref="bodyEl" class="t-body">
      <div v-for="m in msgs" :key="m.id" class="bubble-row" :class="{ mine: adminMode ? m.is_admin : m.is_mine }">
        <div class="bubble" :class="{ staff: adminMode ? m.is_admin : m.is_mine, admin: m.is_admin }">
          <p v-if="m.is_admin" class="b-role">管理员</p>
          <!-- 附件：图片缩略 / 文件卡片 -->
          <template v-if="m.file">
            <a v-if="m.file.is_image && imgUrl[m.file.id]" class="img-att"
               :href="imgUrl[m.file.id]" target="_blank" :title="`${m.file.name}（点击查看原图）`">
              <img :src="imgUrl[m.file.id]" alt="附件图片">
            </a>
            <a v-else class="file-att" :href="`/api/tickets/files/${m.file.id}`" target="_blank">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z" /><path d="M13 3v6h6" /></svg>
              <span class="f-name">{{ m.file.name }}</span>
              <span class="f-size">{{ fmtSize(m.file.size) }}</span>
            </a>
          </template>
          <!-- 文本：Markdown 渲染（代码块/表格/换行），消毒防注入 -->
          <div v-if="m.content" class="b-text md" v-html="renderMd(m.content)" />
          <p class="b-time">{{ fmtTime(m.created_at) }}</p>
        </div>
      </div>
      <p v-if="!msgs.length" class="t-empty text-dim">加载中…</p>
    </div>

    <div v-if="info?.status === 'open'" class="t-input">
      <input ref="fileEl" type="file" hidden @change="onPick">
      <button class="att-btn" :disabled="uploading" title="发送图片或文件（≤10MB），支持直接 Ctrl+V 粘贴截图"
              @click="fileEl?.click()">
        <svg v-if="!uploading" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.4 11.05 12.25 20.2a5.5 5.5 0 0 1-7.78-7.78l8.49-8.48a3.67 3.67 0 0 1 5.18 5.18l-8.48 8.49a1.83 1.83 0 0 1-2.59-2.59l7.78-7.78" /></svg>
        <span v-else class="spin" />
      </button>
      <textarea v-model="draft" rows="2" @paste="onPaste"
                :placeholder="adminMode ? '以管理员身份回复…支持 Markdown 与代码块，Enter 发送，Shift+Enter 换行' : '描述你的问题，可直接粘贴代码（```包裹渲染更佳）与截图，Enter 发送'"
                @keydown.enter.exact.prevent="send" />
      <UIButton :loading="sending" @click="send">发送</UIButton>
    </div>
    <p v-else class="t-closed text-dim">工单已关闭，如需继续请重新提交工单。</p>
  </UIModal>
</template>

<style scoped>
.t-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; font-size: 12.5px; }
.t-user { color: var(--text-2); }
.t-head .danger { margin-left: auto; color: var(--danger, #e5484d); }
.t-body {
  height: 340px; overflow-y: auto; padding: 6px 4px; border: 1px solid var(--line);
  border-radius: 12px; background: var(--bg); display: flex; flex-direction: column; gap: 12px;
}
.bubble-row { display: flex; }
.bubble-row.mine { justify-content: flex-end; }
.bubble {
  max-width: 80%; border-radius: 14px; padding: 9px 13px 7px;
  background: var(--panel); border: 1px solid var(--line);
}
.bubble.staff { background: var(--primary-soft, #e8f6f1); border-color: color-mix(in srgb, var(--primary) 24%, transparent); }
.b-role { margin: 0 0 4px; font-size: 11px; font-weight: 700; color: var(--primary-strong); }
.b-time { margin: 4px 0 0; font-size: 10.5px; color: var(--text-dim); text-align: right; }
.t-empty { text-align: center; margin: auto; font-size: 12.5px; }

/* 图片附件 */
.img-att { display: block; border-radius: 10px; overflow: hidden; border: 1px solid var(--line); background: #fff; }
.img-att img { display: block; max-width: 260px; max-height: 200px; object-fit: contain; }

/* 文件附件卡片 */
.file-att {
  display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-radius: 10px;
  background: color-mix(in srgb, var(--primary) 7%, transparent); border: 1px solid color-mix(in srgb, var(--primary) 22%, transparent);
  color: var(--text); text-decoration: none; max-width: 280px;
}
.file-att:hover { border-color: var(--primary); }
.f-name { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.f-size { font-size: 11px; color: var(--text-dim); flex: none; }

/* Markdown 消息文本 */
.b-text :deep(p) { margin: 0; }
.b-text :deep(pre) {
  background: #0e1a17; color: #d7efe7; border-radius: 8px; padding: 10px 12px;
  overflow-x: auto; font-size: 12px; line-height: 1.65; margin: 6px 0;
  font-family: ui-monospace, Consolas, monospace;
}
.b-text :deep(code) { font-family: ui-monospace, Consolas, monospace; font-size: 12px; background: color-mix(in srgb, var(--primary) 10%, transparent); border-radius: 4px; padding: 1px 5px; }
.b-text :deep(pre code) { background: none; padding: 0; }
.b-text :deep(ul), .b-text :deep(ol) { margin: 6px 0; padding-left: 20px; }
.b-text :deep(a) { color: var(--primary-strong); }
.b-text :deep(table) { border-collapse: collapse; font-size: 12px; margin: 6px 0; }
.b-text :deep(th), .b-text :deep(td) { border: 1px solid var(--line); padding: 4px 8px; }

/* 输入区 */
.t-input { display: flex; gap: 10px; margin-top: 12px; align-items: flex-end; }
.att-btn {
  flex: none; width: 38px; height: 38px; border-radius: 10px; border: 1px solid var(--line);
  background: var(--panel); color: var(--text-2); cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all .15s;
}
.att-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--primary-strong); }
.att-btn:disabled { opacity: .55; cursor: wait; }
.spin { width: 14px; height: 14px; border: 2px solid var(--line); border-top-color: var(--primary); border-radius: 50%; animation: rot .8s linear infinite; }
@keyframes rot { to { transform: rotate(360deg); } }
.t-input textarea {
  flex: 1; resize: none; border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  padding: 9px 12px; font-size: 13px; font-family: inherit; color: var(--text); line-height: 1.6;
}
.t-input textarea:focus { outline: none; border-color: var(--primary); }
.t-closed { font-size: 12.5px; text-align: center; margin: 12px 0 0; }
</style>
