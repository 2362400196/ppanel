<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, errText } from '../../api/client'
import { toastErr, toastOk } from '../../components/ui/toast'
import UIButton from '../../components/ui/UIButton.vue'
import UIInput from '../../components/ui/UIInput.vue'

const props = defineProps({ instance: { type: Object, required: true } })

const hostname = ref('')
const hostDraft = ref('')
const saving = ref(false)

async function load() {
  try {
    const { data } = await api.get(`/instances/${props.instance.id}/domain`)
    hostname.value = data.hostname || ''
    hostDraft.value = hostname.value
  } catch (e) {
    toastErr(errText(e))
  }
}

const dirty = computed(() => hostDraft.value.trim() !== hostname.value)
const domainUrl = computed(() => {
  if (!hostname.value) return ''
  const port = location.port ? `:${location.port}` : ''
  return `http://${hostname.value}${port}`
})

async function save() {
  saving.value = true
  try {
    const { data } = await api.put(`/instances/${props.instance.id}/domain`, { hostname: hostDraft.value.trim() })
    hostname.value = data.hostname || ''
    hostDraft.value = hostname.value
    toastOk(hostname.value ? `已绑定 ${hostname.value}` : '已解绑域名')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="domain-page">
    <div class="card">
      <div class="card-head">
        <span class="card-title">绑定域名</span>
        <span v-if="hostname" class="ok-tag">已绑定</span>
      </div>
      <div class="row">
        <UIInput v-model="hostDraft" placeholder="myapp.localhost" mono @enter="save" />
        <UIButton :loading="saving" :disabled="!dirty" @click="save">保存</UIButton>
      </div>
      <p v-if="domainUrl" class="url mono">{{ domainUrl }}</p>
      <div class="steps">
        <div class="step"><span class="no">1</span>把域名 A 记录 / CNAME 解析到主控服务器 IP</div>
        <div class="step"><span class="no">2</span>在上方填入域名并保存，主控会按 Host 自动转发到实例端口</div>
        <div class="step"><span class="no">3</span>本机开发测试可用 *.localhost（浏览器自动指向 127.0.0.1）</div>
      </div>
      <p class="hint text-dim">留空保存即解除绑定；一条实例只能绑定一个域名，域名冲突会提示。</p>
    </div>
  </div>
</template>

<style scoped>
.domain-page { max-width: 640px; }
.card { padding: 20px; }
.card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.card-title { font-size: 14px; font-weight: 700; }
.ok-tag { font-size: 12px; color: var(--primary-strong); }
.row { display: flex; gap: 8px; margin-bottom: 10px; }
.url { font-size: 13px; color: var(--primary-strong); margin-bottom: 12px; word-break: break-all; }
.steps { display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }
.step { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--text-2); }
.no {
  width: 18px; height: 18px; border-radius: 50%; flex-shrink: 0;
  background: var(--primary-soft); color: var(--primary-deep);
  font-size: 11px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.hint { font-size: 11px; }
</style>
