<script setup>
import { ref, watch } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from './ui/toast'
import UIModal from './ui/UIModal.vue'
import UIButton from './ui/UIButton.vue'
import UIInput from './ui/UIInput.vue'
import UISelect from './ui/UISelect.vue'
import UITextarea from './ui/UITextarea.vue'
import { startTask } from '../api/tasks'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['update:open', 'created'])

const images = ref([])
const name = ref('')
const image = ref('')
const cpu = ref('1')
const mem = ref('512')
const disk = ref('2048')
const startCmd = ref('python main.py')
const submitting = ref(false)

const cpuOptions = [
  { value: '0.5', label: '0.5 核' },
  { value: '1', label: '1 核' },
  { value: '2', label: '2 核' },
  { value: '4', label: '4 核' }
]
const memOptions = [
  { value: '256', label: '256 MB' },
  { value: '512', label: '512 MB' },
  { value: '1024', label: '1024 MB' },
  { value: '2048', label: '2048 MB' },
  { value: '4096', label: '4096 MB' }
]
const diskOptions = [
  { value: '1024', label: '1 GB' },
  { value: '2048', label: '2 GB' },
  { value: '5120', label: '5 GB' },
  { value: '10240', label: '10 GB' }
]

watch(() => props.open, async v => {
  if (!v) return
  name.value = ''
  startCmd.value = 'python main.py'
  try {
    const { data } = await api.get('/images')
    images.value = data
    if (!image.value || !data.includes(image.value)) {
      image.value = data.find(i => i.includes('3.11')) || data[0] || ''
    }
  } catch (e) {
    toastErr(errText(e))
  }
})

// 切换运行环境时联动默认启动命令
watch(image, v => {
  if (v.startsWith('php:')) startCmd.value = 'php -S 0.0.0.0:8000 -t /app'
  else if (v.startsWith('node:')) startCmd.value = 'node index.js'
  else if (v.startsWith('golang:')) startCmd.value = '[ -f go.mod ] || go mod init ppanel-app; go build -o app . && ./app'
  else startCmd.value = 'python main.py'
})

async function submit() {
  if (!name.value.trim()) return toastErr('请输入实例名称')
  submitting.value = true
  try {
    const tid = startTask()  // 实例创建在主控落库+节点建容器，全程终端日志
    const { data } = await api.post('/instances', {
      name: name.value.trim(),
      image: image.value,
      cpu_limit: parseFloat(cpu.value),
      mem_limit: parseInt(mem.value),
      disk_quota: parseInt(disk.value),
      start_cmd: startCmd.value.trim()
    }, { headers: { 'X-Task-Id': tid } })
    toastOk(`实例「${data.name}」创建成功，外部端口 ${data.ext_port}`)
    emit('update:open', false)
    emit('created', data)
  } catch (e) {
    toastErr(errText(e))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <UIModal :open="open" title="创建实例" width="440px" persistent @update:open="$emit('update:open', $event)">
    <div class="form">
      <label class="field">
        <span class="label">实例名称</span>
        <UIInput v-model="name" placeholder="如 my-api" />
      </label>
      <label class="field">
        <span class="label">运行环境</span>
        <UISelect v-model="image" :options="images" :disabled="!images.length" />
      </label>
      <div class="grid3">
        <label class="field">
          <span class="label">CPU</span>
          <UISelect v-model="cpu" :options="cpuOptions" />
        </label>
        <label class="field">
          <span class="label">内存</span>
          <UISelect v-model="mem" :options="memOptions" />
        </label>
        <label class="field">
          <span class="label">磁盘配额</span>
          <UISelect v-model="disk" :options="diskOptions" />
        </label>
      </div>
      <label class="field">
        <span class="label">启动命令</span>
        <UITextarea v-model="startCmd" :rows="2" mono placeholder="uvicorn main:app --host 0.0.0.0 --port 8000" />
        <span class="tip">应用需在容器内监听 8000 端口，该端口会映射到外部端口</span>
      </label>
      <p v-if="submitting" class="creating">正在创建实例，首次需要拉取镜像，请耐心等待…</p>
    </div>
    <template #footer>
      <UIButton type="ghost" :disabled="submitting" @click="$emit('update:open', false)">取消</UIButton>
      <UIButton :loading="submitting" @click="submit">创建实例</UIButton>
    </template>
  </UIModal>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.label { font-size: 12px; font-weight: 600; color: var(--text-2); }
.grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
.tip { font-size: 11px; color: var(--text-dim); }
.creating { font-size: 12px; color: var(--primary-strong); animation: blink 1.2s ease-in-out infinite; }
@keyframes blink { 50% { opacity: .45; } }
</style>
