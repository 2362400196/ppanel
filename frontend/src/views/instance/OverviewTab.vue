<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../../api/client'
import { toastErr, toastOk } from '../../components/ui/toast'
import UIButton from '../../components/ui/UIButton.vue'
import UITextarea from '../../components/ui/UITextarea.vue'
import UIInput from '../../components/ui/UIInput.vue'
import ResourceChart from '../../components/ResourceChart.vue'

const props = defineProps({ instance: { type: Object, required: true } })
const emit = defineEmits(['changed'])

const cpuHistory = ref([])
const memHistory = ref([])
const stats = ref({ running: false, cpu_percent: 0, mem_usage_mb: 0, mem_limit_mb: props.instance.mem_limit, mem_percent: 0 })

const startCmd = ref(props.instance.start_cmd)
const note = ref(props.instance.note)
const savingCmd = ref(false)
const cmdDirty = ref(false)

let timer

async function poll() {
  try {
    const { data } = await api.get(`/instances/${props.instance.id}/stats`)
    stats.value = data
    cpuHistory.value.push(data.cpu_percent)
    memHistory.value.push(data.mem_percent)
    if (cpuHistory.value.length > 120) cpuHistory.value.shift()
    if (memHistory.value.length > 120) memHistory.value.shift()
  } catch { /* 静默重试 */ }
}

async function saveCmd() {
  savingCmd.value = true
  try {
    await api.patch(`/instances/${props.instance.id}`, { start_cmd: startCmd.value, note: note.value })
    toastOk('启动命令已保存')
    cmdDirty.value = false
    emit('changed')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    savingCmd.value = false
  }
}

onMounted(() => { poll(); timer = setInterval(poll, 2000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="overview">
    <div class="chart-card card">
      <div class="card-head">
        <span class="card-title">资源占用</span>
        <span class="live" :class="{ off: !stats.running }">{{ stats.running ? '实时采样中' : '容器未运行' }}</span>
      </div>
      <div class="chart-box">
        <ResourceChart
          :cpu-history="cpuHistory"
          :mem-history="memHistory"
          :mem-usage-mb="stats.mem_usage_mb"
        />
      </div>
    </div>

    <div class="side">
      <div class="info-card card">
        <div class="card-head"><span class="card-title">实例信息</span></div>
        <div class="info-grid">
          <div class="i-row"><span class="k">镜像</span><span class="v mono">{{ instance.image }}</span></div>
          <div class="i-row"><span class="k">外部端口</span><span class="v mono">{{ instance.ext_port }}</span></div>
          <div class="i-row"><span class="k">CPU / 内存</span><span class="v">{{ instance.cpu_limit }} 核 / {{ instance.mem_limit }}MB</span></div>
          <div class="i-row"><span class="k">磁盘配额</span><span class="v">{{ (instance.disk_quota / 1024).toFixed(0) }}GB</span></div>
          <div class="i-row wide"><span class="k">挂载目录</span><span class="v mono small">{{ instance.host_dir || '创建后分配' }}</span></div>
          <div class="i-row wide"><span class="k">访问方式</span><span class="v mono small">http://服务器IP:{{ instance.ext_port }}</span></div>
        </div>
      </div>

      <div class="cmd-card card">
        <div class="card-head">
          <span class="card-title">启动命令</span>
          <span class="tip">应用需监听 8000 端口</span>
        </div>
        <UITextarea
          v-model="startCmd"
          :rows="3"
          mono
          placeholder="uvicorn main:app --host 0.0.0.0 --port 8000"
          @input="cmdDirty = true"
        />
        <div class="cmd-foot">
          <UIInput v-model="note" placeholder="备注（可选）" @input="cmdDirty = true" />
          <UIButton :loading="savingCmd" :disabled="!cmdDirty" @click="saveCmd">保存</UIButton>
        </div>
        <p class="hint">保存后需重启实例生效；运行中修改会要求先停止实例。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overview { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; align-items: start; }
.card { padding: 16px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.card-title { font-size: 14px; font-weight: 700; }
.tip { font-size: 11px; color: var(--text-dim); }
.live { font-size: 11px; color: var(--primary-strong); display: flex; align-items: center; gap: 5px; }
.live::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--primary); animation: pulse 1.4s ease-in-out infinite; }
.live.off { color: var(--text-dim); }
.live.off::before { background: var(--text-dim); animation: none; }
@keyframes pulse { 50% { opacity: .3; } }
.chart-box { height: 240px; }

.side { display: flex; flex-direction: column; gap: 16px; }
.info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.i-row { display: flex; flex-direction: column; gap: 3px; }
.i-row.wide { grid-column: span 2; }
.k { font-size: 11px; color: var(--text-dim); }
.v { font-size: 13px; color: var(--text); word-break: break-all; }
.v.small { font-size: 12px; color: var(--text-2); }

.cmd-card { display: flex; flex-direction: column; gap: 10px; }
.cmd-foot { display: flex; gap: 8px; }
.hint { font-size: 11px; color: var(--text-dim); }

@media (max-width: 960px) {
  .overview { grid-template-columns: 1fr; }
}
</style>
