<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../../api/client'
import { fmtTime } from '../../utils/format'
import { toastErr } from '../../components/ui/toast'

const props = defineProps({ instance: { type: Object, required: true } })

const ops = ref([])

const ACTION_LABELS = {
  create_instance: '创建实例', start: '启动', stop: '停止', restart: '重启',
  update_instance: '更新配置', delete_instance: '删除实例',
  bind_domain: '绑定域名', unbind_domain: '解绑域名',
  install_deps: '安装依赖', upload_file: '上传文件'
}

async function load() {
  try {
    const { data } = await api.get(`/instances/${props.instance.id}/ops`, { params: { limit: 20 } })
    ops.value = data
  } catch (e) {
    toastErr(errText(e))
  }
}

let timer
onMounted(() => {
  load()
  timer = setInterval(load, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="ops-page">
    <div class="card">
      <div class="card-head">
        <span class="card-title">操作记录</span>
        <span class="text-dim hint">最近 {{ ops.length }} 条 · 10s 自动刷新</span>
      </div>
      <div v-if="!ops.length" class="empty text-dim">暂无操作记录</div>
      <div v-else class="list">
        <div v-for="op in ops" :key="op.id" class="row">
          <span class="dot" />
          <span class="action">{{ ACTION_LABELS[op.action] || op.action }}</span>
          <span class="detail mono">{{ op.detail }}</span>
          <span class="time">{{ fmtTime(op.created_at) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ops-page { max-width: 760px; }
.card { padding: 20px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.card-title { font-size: 14px; font-weight: 700; }
.hint { font-size: 11px; }
.empty { text-align: center; padding: 32px 0; font-size: 13px; }
.list { display: flex; flex-direction: column; }
.row {
  display: flex; align-items: center; gap: 12px; padding: 9px 4px;
  border-bottom: 1px dashed var(--line); font-size: 13px;
  animation: slide-in .25s ease;
}
@keyframes slide-in { from { opacity: 0; transform: translateY(4px); } }
.row:last-child { border-bottom: none; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--primary); flex-shrink: 0; opacity: .75; }
.action { font-weight: 600; color: var(--text); flex-shrink: 0; }
.detail { color: var(--text-dim); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.time { color: var(--text-dim); flex-shrink: 0; font-size: 12px; }
</style>
