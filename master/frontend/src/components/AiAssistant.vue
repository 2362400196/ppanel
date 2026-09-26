<script setup>
// 全局 AI 助手：右下角悬浮球 + 侧滑抽屉，任何管理页面可随时打开。
// DeepSeek 流式对话（SSE 经主控转发，Key 只存服务端）。
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from './ui/toast'
import UIButton from './ui/UIButton.vue'
import UIInput from './ui/UIInput.vue'
import UIModal from './ui/UIModal.vue'
import UISelect from './ui/UISelect.vue'
import ConfirmDialog from './ui/ConfirmDialog.vue'

const open = ref(false)

const cfg = ref({ has_key: false, key_masked: '' })
const cfgOpen = ref(false)
const keyInput = ref('')
const savingKey = ref(false)

const model = ref('deepseek-chat')
const modelOpts = [
  { value: 'deepseek-chat', label: 'V3 通用' },
  { value: 'deepseek-reasoner', label: 'R1 深度思考' },
]

const messages = ref([])   // [{ role, content, thinking?, done? }]
const input = ref('')
const streaming = ref(false)
const streamErr = ref('')
let abortCtrl = null

const listEl = ref(null)
const stickBottom = ref(true)

// AI 回复 Markdown 渲染（GFM 表格 + 单换行转 <br>；DOMPurify 防注入）
marked.setOptions({ gfm: true, breaks: true })
function mdRender(text) {
  if (!text) return ''
  return DOMPurify.sanitize(marked.parse(text))
}

async function loadCfg() {
  try {
    const { data } = await api.get('/ai/config')
    cfg.value = data
  } catch { /* 配置读取失败不阻塞 */ }
}

async function saveKey() {
  savingKey.value = true
  try {
    await api.put('/ai/config', { api_key: keyInput.value.trim() })
    toastOk('API Key 已保存')
    cfgOpen.value = false
    keyInput.value = ''
    loadCfg()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    savingKey.value = false
  }
}

const SUGGESTS = [
  '帮我对比一下现在的商品配置',
  '看看最近有没有实例崩溃',
  '测一下所有节点是否在线',
]

const TOOL_LABEL = {
  list_nodes: '查询节点',
  add_node: '创建节点',
  list_plans: '查询商品',
  add_plan: '创建商品',
  compare_nodes: '节点对比',
  list_instances: '查询实例',
  list_backups: '查询备份',
  create_backup: '创建备份',
  restore_backup: '恢复备份',
  instance_detail: '实例详情',
  instance_power: '实例电源操作',
  probe_instance: '测连通',
  node_test: '节点测活',
  recent_events: '查询异常事件',
  list_users: '查询用户',
  create_user: '创建用户',
}

function scrollBottom(force = false) {
  const el = listEl.value
  if (!el) return
  if (!force && !stickBottom.value) return
  nextTick(() => { el.scrollTop = el.scrollHeight })
}

function onScroll() {
  const el = listEl.value
  if (!el) return
  stickBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function stopStream() {
  abortCtrl?.abort()
  abortCtrl = null
}

// 新对话：清空上下文（带确认）
const newOpen = ref(false)
const ctxRounds = computed(() =>
  messages.value.filter(m => m.role === 'user' || (m.role === 'assistant' && m.content)).length)
const ctxHot = computed(() => ctxRounds.value >= 32)  // 接近后端 40 轮上限

function newChat() {
  stopStream()
  messages.value = []
  streamErr.value = ''
  newOpen.value = false
  stickBottom.value = true
}

// 流式发送：fetch + ReadableStream 手动解析 SSE（axios 不支持流式）
async function send(text) {
  const content = (text ?? input.value).trim()
  if (!content || streaming.value) return
  if (!cfg.value.has_key) { cfgOpen.value = true; return }
  input.value = ''
  streamErr.value = ''
  messages.value.push({ role: 'user', content })
  const reply = { role: 'assistant', content: '', thinking: '', thinkingDone: false, steps: [], done: false }
  messages.value.push(reply)
  streaming.value = true
  stickBottom.value = true
  scrollBottom(true)

  abortCtrl = new AbortController()
  try {
    // 只回传真实消息；正在生成的 assistant 占位因 content 为空已被 filter 排除
    const history = messages.value
      .filter(m => m.role === 'user' || (m.role === 'assistant' && m.content))
      .map(m => ({ role: m.role, content: m.content }))
    const resp = await fetch('/api/ai/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('ppanel_token') || ''}`,
      },
      body: JSON.stringify({ model: model.value, messages: history }),
      signal: abortCtrl.signal,
    })
    if (!resp.ok) {
      let detail = `请求失败（${resp.status}）`
      try { detail = (await resp.json()).detail || detail } catch { /* ignore */ }
      throw new Error(detail)
    }
    const reader = resp.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop()
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const payload = line.slice(6).trim()
        if (!payload || payload === '[DONE]') continue
        let obj
        try { obj = JSON.parse(payload) } catch { continue }
        if (obj.error) throw new Error(obj.error)
        // 工具执行事件：同一工具的 running → ok/fail 更新同一步骤
        if (obj.tool) {
          const last = reply.steps[reply.steps.length - 1]
          if (obj.tool.status === 'running' && last && last.name === obj.tool.name && last.status === 'running') {
            continue
          }
          if (obj.tool.status !== 'running' && last && last.name === obj.tool.name && last.status === 'running') {
            last.status = obj.tool.status
            last.detail = obj.tool.detail || ''
          } else {
            reply.steps.push({ name: obj.tool.name, status: obj.tool.status, detail: obj.tool.detail || '' })
          }
          scrollBottom()
          continue
        }
        const delta = obj.choices?.[0]?.delta || {}
        if (delta.reasoning_content) {
          reply.thinking += delta.reasoning_content
          reply.thinkingDone = false
        }
        if (delta.content) {
          reply.content += delta.content
          if (!reply.thinkingDone && reply.thinking) reply.thinkingDone = true
        }
        scrollBottom()
      }
    }
  } catch (e) {
    if (e?.name === 'AbortError') {
      if (reply.content) reply.content += '\n\n（已停止生成）'
    } else {
      streamErr.value = e?.message || '对话失败'
      toastErr(streamErr.value)
    }
  } finally {
    reply.done = true
    reply.thinkingDone = true
    streaming.value = false
    abortCtrl = null
    scrollBottom()
  }
}

function onEnter(e) {
  if (e.shiftKey) return
  e.preventDefault()
  send()
}

watch(open, v => { if (v) { loadCfg(); scrollBottom(true) } })
onMounted(loadCfg)
</script>

<template>
  <!-- 悬浮球：右下角，任何页面可打开 -->
  <button class="ai-fab" :class="{ on: open, busy: streaming }" title="AI 助手"
          @click="open = !open">
    <svg v-if="streaming && !open" class="fab-pause" viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><rect x="7" y="5" width="3.6" height="14" rx="1.2" /><rect x="13.4" y="5" width="3.6" height="14" rx="1.2" /></svg>
    <svg v-else class="fab-ico" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 2.5 13.8 8.6a2 2 0 0 0 1.3 1.3l6.1 1.8a.8.8 0 0 1 0 1.55l-6.1 1.8a2 2 0 0 0-1.3 1.3L12 22.5l-1.8-6.1a2 2 0 0 0-1.3-1.3l-6.1-1.8a.8.8 0 0 1 0-1.55l6.1-1.8a2 2 0 0 0 1.3-1.3z" />
    </svg>
    <span v-if="streaming" class="fab-ring" />
  </button>

  <!-- 侧滑抽屉 -->
  <Teleport to="body">
    <Transition name="drawer">
      <div v-if="open" class="ai-mask" @click.self="open = false">
        <div class="ai-panel">
          <div class="ai-head">
            <div class="ai-brand">
              <span class="ai-logo">P</span>
              <div class="ai-tt">
                <b>AI 助手</b>
                <span class="ai-sub">DeepSeek · 可直接操作平台</span>
              </div>
            </div>
            <div class="ai-actions">
              <UISelect v-model="model" :options="modelOpts" style="width:132px" />
              <button class="ico-btn" :title="cfg.has_key ? `密钥 ${cfg.key_masked}` : '配置密钥'" @click="cfgOpen = true">
                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="15" r="4.5" /><path d="M11 12 21 2m-4 2 3 3m-6 0 2.5 2.5" /></svg>
              </button>
              <button class="ico-btn" title="新对话" :disabled="!messages.length" @click="newOpen = true">
                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14" /></svg>
              </button>
              <button class="ico-btn" title="收起" @click="open = false">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18" /></svg>
              </button>
            </div>
          </div>

          <div ref="listEl" class="ai-list" @scroll="onScroll">
            <!-- 欢迎态 -->
            <div v-if="!messages.length" class="hello">
              <div class="hello-logo">P</div>
              <div class="hello-title">有什么可以帮你？</div>
              <div class="hello-sub">运维排查、创建节点商品、备份恢复…直接提问</div>
              <div class="hello-tips">
                <button v-for="s in SUGGESTS" :key="s" class="tip-card" @click="send(s)">{{ s }}</button>
              </div>
            </div>

            <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
              <div v-if="m.role === 'assistant'" class="avatar">P</div>
              <div class="bubble">
                <div v-if="m.steps && m.steps.length" class="steps">
                  <div v-for="(s, si) in m.steps" :key="si" class="step" :class="s.status">
                    <span class="step-ico">
                      <span v-if="s.status === 'running'" class="step-spin" />
                      <template v-else>{{ s.status === 'ok' ? '✓' : '✕' }}</template>
                    </span>
                    <span class="step-name">{{ TOOL_LABEL[s.name] || s.name }}</span>
                    <span v-if="s.detail" class="step-detail" :class="s.status">{{ s.detail }}</span>
                  </div>
                </div>
                <div v-if="m.thinking" class="think" :class="{ open: !m.thinkingDone && streaming && i === messages.length - 1 }">
                  <button class="think-head" @click="m._openThink = !m._openThink">
                    <span class="think-dot" />
                    {{ m.thinkingDone ? '已深度思考' : '思考中…' }}
                    <span class="think-chev" :class="{ up: m._openThink }">‹</span>
                  </button>
                  <pre v-if="m._openThink || (!m.thinkingDone && streaming && i === messages.length - 1)" class="think-body">{{ m.thinking }}</pre>
                </div>
                <div v-if="m.role === 'assistant' && (m.content || !(m.steps && m.steps.length))"
                     class="content md" :class="{ typing: !m.done && i === messages.length - 1 }"
                     v-html="m.content ? mdRender(m.content) : '…'" />
                <div v-else-if="m.role === 'user' || (m.content || !(m.steps && m.steps.length))"
                     class="content" :class="{ typing: !m.done && i === messages.length - 1 }">{{ m.content || '…' }}</div>
              </div>
              <div v-if="m.role === 'user'" class="avatar me">我</div>
            </div>
          </div>

          <p v-if="streamErr" class="ai-err">{{ streamErr }}</p>
          <div class="ai-input">
            <textarea v-model="input" rows="1" class="ai-textarea"
                      :placeholder="cfg.has_key ? '输入问题，Enter 发送，Shift+Enter 换行' : '请先配置 DeepSeek API Key'"
                      @keydown.enter="onEnter" />
            <UIButton v-if="!streaming" :disabled="!input.trim() || !cfg.has_key" @click="send()">发送</UIButton>
            <UIButton v-else type="ghost" @click="stopStream">停止</UIButton>
          </div>
          <div class="ai-foot">
            <span class="ctx-chip" :class="{ hot: ctxHot }">上下文 {{ ctxRounds }} 轮</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>

  <ConfirmDialog v-model:open="newOpen" title="开始新对话"
                 :message="`将清空当前上下文（${ctxRounds} 条消息），AI 不再记得之前的内容，此操作不可恢复。`"
                 confirm-text="清空并开始" @confirm="newChat" />

  <UIModal v-model:open="cfgOpen" title="配置 DeepSeek API Key" width="520px">
    <div class="cfg">
      <UIInput v-model="keyInput" placeholder="sk-…" />
      <p class="cfg-tip">Key 在 DeepSeek 开放平台 platform.deepseek.com 创建，仅保存在主控服务端，不会下发到浏览器。
        模型调用按 DeepSeek 官方计费。</p>
      <div class="cfg-foot">
        <UIButton type="ghost" @click="cfgOpen = false">取消</UIButton>
        <UIButton :loading="savingKey" :disabled="!keyInput.trim()" @click="saveKey">保存</UIButton>
      </div>
    </div>
  </UIModal>
</template>

<style scoped>
/* ---------- 悬浮球 ---------- */
.ai-fab {
  position: fixed; right: 26px; bottom: 26px; z-index: 900;
  width: 52px; height: 52px; border-radius: 50%; border: none; cursor: pointer;
  display: grid; place-items: center; color: #fff;
  background: linear-gradient(135deg, var(--primary), color-mix(in srgb, var(--primary) 55%, #0e7a63));
  box-shadow: 0 10px 28px color-mix(in srgb, var(--primary) 45%, transparent), 0 2px 8px rgb(0 0 0 / 12%);
  transition: transform .2s ease, box-shadow .2s ease;
  animation: fab-breath 3.4s ease-in-out infinite;
}
.ai-fab:hover { transform: translateY(-3px) scale(1.05); }
.ai-fab.on { transform: rotate(90deg); animation: none; }
.ai-fab.busy { animation: fab-pulse 1.1s ease-in-out infinite; }
@keyframes fab-breath {
  0%, 100% { box-shadow: 0 10px 28px color-mix(in srgb, var(--primary) 45%, transparent), 0 0 0 0 color-mix(in srgb, var(--primary) 30%, transparent); }
  50% { box-shadow: 0 10px 28px color-mix(in srgb, var(--primary) 45%, transparent), 0 0 0 9px transparent; }
}
@keyframes fab-pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.06); } }
.fab-ring {
  position: absolute; inset: -4px; border-radius: 50%;
  border: 2px solid color-mix(in srgb, var(--primary) 60%, transparent);
  animation: ring-spin 1.2s linear infinite;
  border-top-color: transparent; border-bottom-color: transparent;
}
@keyframes ring-spin { to { transform: rotate(360deg); } }

/* ---------- 抽屉 ---------- */
.ai-mask {
  position: fixed; inset: 0; z-index: 1000;
  background: rgb(12 20 17 / 32%); backdrop-filter: blur(3px);
}
.ai-panel {
  position: absolute; top: 0; right: 0; height: 100%; width: 540px; max-width: 94vw;
  background: color-mix(in srgb, var(--bg) 88%, #fff 12%);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  border-left: 1px solid var(--line);
  box-shadow: -24px 0 60px rgb(8 30 24 / 18%);
  display: flex; flex-direction: column;
}
.drawer-enter-active, .drawer-leave-active { transition: opacity .22s ease; }
.drawer-enter-active .ai-panel, .drawer-leave-active .ai-panel { transition: transform .26s cubic-bezier(.22,.9,.34,1); }
.drawer-enter-from, .drawer-leave-to { opacity: 0; }
.drawer-enter-from .ai-panel, .drawer-leave-to .ai-panel { transform: translateX(60px); }

.ai-head {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 14px 16px; border-bottom: 1px solid var(--line); flex: none;
}
.ai-brand { display: flex; align-items: center; gap: 10px; }
.ai-logo {
  width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
  background: linear-gradient(135deg, var(--primary), color-mix(in srgb, var(--primary) 60%, #0e7a63));
  color: #fff; font-weight: 800; font-size: 16px;
}
.ai-tt { display: flex; flex-direction: column; line-height: 1.25; }
.ai-tt b { font-size: 14px; }
.ai-sub { font-size: 11px; color: var(--text-dim); }
.ai-actions { display: flex; align-items: center; gap: 6px; }
.ico-btn {
  width: 30px; height: 30px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--text-2); cursor: pointer;
  display: grid; place-items: center; transition: all .15s;
}
.ico-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--primary-strong); }
.ico-btn:disabled { opacity: .45; cursor: not-allowed; }

.ai-list { flex: 1; overflow-y: auto; padding: 16px 14px 8px; }
.hello { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 9px; }
.hello-logo {
  width: 58px; height: 58px; border-radius: 18px; display: grid; place-items: center;
  background: linear-gradient(135deg, var(--primary), color-mix(in srgb, var(--primary) 60%, #0e7a63));
  color: #fff; font-size: 27px; font-weight: 800;
  box-shadow: 0 14px 34px color-mix(in srgb, var(--primary) 35%, transparent);
  animation: float 3.2s ease-in-out infinite;
}
@keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-7px); } }
.hello-title { font-size: 17px; font-weight: 700; }
.hello-sub { font-size: 12.5px; color: var(--text-dim); }
.hello-tips { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; width: 100%; max-width: 330px; }
.tip-card {
  border: 1px solid var(--line); background: var(--panel); border-radius: 12px;
  padding: 10px 14px; font-size: 13px; color: var(--text-2); cursor: pointer; font-family: inherit;
  text-align: left; transition: all .15s;
}
.tip-card:hover { border-color: var(--primary); color: var(--primary-strong); transform: translateX(3px); }

.msg { display: flex; gap: 8px; margin-bottom: 16px; }
.msg.user { justify-content: flex-end; }
.avatar {
  flex: none; width: 28px; height: 28px; border-radius: 9px; display: grid; place-items: center;
  background: linear-gradient(135deg, var(--primary), color-mix(in srgb, var(--primary) 60%, #0e7a63));
  color: #fff; font-size: 13px; font-weight: 800; margin-top: 2px;
}
.avatar.me { background: var(--panel); border: 1px solid var(--line); color: var(--text-2); font-size: 11px; }
.bubble {
  max-width: 82%; border-radius: 14px; padding: 10px 13px;
  background: var(--panel); border: 1px solid var(--line);
  font-size: 13.5px; line-height: 1.7;
}
.msg.user .bubble { background: var(--primary-soft, #e8f6f1); border-color: color-mix(in srgb, var(--primary) 22%, transparent); }

.steps { display: flex; flex-direction: column; gap: 5px; margin-bottom: 8px; }
.step {
  display: flex; align-items: center; gap: 7px; font-size: 11.5px;
  border: 1px dashed var(--line); border-radius: 8px; padding: 5px 9px; background: var(--bg);
}
.step-ico { flex: none; width: 15px; text-align: center; font-weight: 800; }
.step.ok .step-ico { color: var(--ok, #30a46c); }
.step.fail .step-ico { color: var(--danger, #e5484d); }
.step-spin {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  border: 2px solid var(--line); border-top-color: var(--primary);
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.step-name { font-weight: 700; color: var(--text-2); }
.step-detail { color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step-detail.ok { color: var(--ok, #30a46c); }
.step-detail.fail { color: var(--danger, #e5484d); }

.think { margin-bottom: 8px; border: 1px solid var(--line); border-radius: 10px; background: var(--bg); overflow: hidden; }
.think-head {
  width: 100%; display: flex; align-items: center; gap: 7px; padding: 7px 10px;
  border: none; background: transparent; cursor: pointer; font-size: 11.5px; color: var(--text-dim); font-family: inherit;
}
.think-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--primary); animation: blink 1s ease-in-out infinite; }
@keyframes blink { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }
.think-chev { margin-left: auto; transition: transform .2s; }
.think-chev.up { transform: rotate(-90deg); }
.think-body {
  margin: 0; padding: 8px 12px; border-top: 1px solid var(--line);
  font-size: 11.5px; line-height: 1.7; color: var(--text-dim);
  white-space: pre-wrap; max-height: 180px; overflow-y: auto; font-family: inherit;
}

.content :deep(p) { margin: 0 0 6px; }
.content :deep(p:last-child) { margin-bottom: 0; }
.content :deep(pre) {
  background: #0e1a17; color: #d7efe7; border-radius: 8px; padding: 10px 12px;
  overflow-x: auto; font-size: 12px; line-height: 1.65; margin: 6px 0;
  font-family: ui-monospace, Consolas, monospace;
}
.content :deep(code) { font-family: ui-monospace, Consolas, monospace; font-size: 12px; background: color-mix(in srgb, var(--primary) 10%, transparent); border-radius: 4px; padding: 1px 5px; }
.content :deep(pre code) { background: none; padding: 0; }
.content :deep(ul), .content :deep(ol) { margin: 6px 0; padding-left: 20px; }
.content :deep(a) { color: var(--primary-strong); }
.content :deep(table) { border-collapse: collapse; font-size: 12px; margin: 6px 0; }
.content :deep(th), .content :deep(td) { border: 1px solid var(--line); padding: 4px 8px; }
.content.typing::after { content: '▍'; color: var(--primary); animation: blink 1s steps(1) infinite; }

.ai-err { flex: none; margin: 0; padding: 8px 16px 0; font-size: 12px; color: var(--danger, #e5484d); }
.ai-input {
  flex: none; display: flex; gap: 8px; align-items: flex-end;
  padding: 12px 14px; border-top: 1px solid var(--line);
}
.ai-textarea {
  flex: 1; resize: none; border: 1px solid var(--line); border-radius: 12px;
  background: var(--panel); padding: 9px 12px; font-size: 13px; font-family: inherit;
  color: var(--text); line-height: 1.6; max-height: 110px;
}
.ai-textarea:focus { outline: none; border-color: var(--primary); }
.ai-foot { flex: none; display: flex; justify-content: flex-end; padding: 0 14px 10px; }
.ctx-chip {
  font-size: 11px; color: var(--text-dim); padding: 3px 9px;
  border: 1px solid var(--line); border-radius: 999px; background: var(--panel);
  font-variant-numeric: tabular-nums;
}
.ctx-chip.hot { color: #b8860b; border-color: color-mix(in srgb, #b8860b 40%, transparent); }

.cfg { display: flex; flex-direction: column; gap: 12px; }
.cfg-tip { margin: 0; font-size: 12px; line-height: 1.7; color: var(--text-dim); }
.cfg-foot { display: flex; justify-content: flex-end; gap: 10px; }
</style>
