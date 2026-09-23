<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITabs from '../components/ui/UITabs.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import FileExplorer from '../components/FileExplorer.vue'
import OverviewTab from './instance/OverviewTab.vue'
import DepsTab from './instance/DepsTab.vue'
import LogsTab from './instance/LogsTab.vue'

const route = useRoute()
const router = useRouter()
const instanceId = Number(route.params.id)

const inst = ref(null)
const tab = ref('overview')
const loading = ref(true)
const delOpen = ref(false)
const acting = ref('')

const tabs = [
  { key: 'overview', label: '概览' },
  { key: 'files', label: '文件' },
  { key: 'deps', label: '依赖' },
  { key: 'logs', label: '日志' }
]

async function load(silent = false) {
  try {
    const { data } = await api.get(`/instances/${instanceId}`)
    // 保留本地编辑状态字段不变（start_cmd 由概览页自行管理）
    const first = !inst.value
    inst.value = data
    if (first) {
      startCmdDraft.value = data.start_cmd
    }
  } catch (e) {
    if (!silent) toastErr(errText(e))
    if (e.response?.status === 404) router.replace('/')
  } finally {
    loading.value = false
  }
}

const startCmdDraft = ref('')

async function act(action) {
  acting.value = action
  try {
    await api.post(`/instances/${instanceId}/${action}`)
    toastOk({ start: '已启动', stop: '已停止', restart: '已重启' }[action] || '操作成功')
    await load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    acting.value = ''
  }
}

async function doDelete() {
  try {
    const { data } = await api.delete(`/instances/${instanceId}`)
    toastOk(data.detail || '已删除')
    router.push('/')
  } catch (e) {
    toastErr(errText(e))
  }
}

let timer
onMounted(() => {
  load()
  timer = setInterval(() => load(true), 5000)
})
onUnmounted(() => clearInterval(timer))
watch(() => route.params.id, v => { if (v && Number(v) !== instanceId) router.go(0) })

const isRunning = computed(() => inst.value?.status === 'running')
</script>

<template>
  <div>
    <div v-if="loading" class="loading text-dim">加载中…</div>
    <template v-else-if="inst">
      <div class="head">
        <div class="head-left">
          <button class="back" @click="router.push('/')">
            <i class="back-icon" /> 返回
          </button>
          <h1 class="page-title">{{ inst.name }}</h1>
          <StatusDot :status="inst.status" />
          <span class="port-chip mono">:{{ inst.ext_port }}</span>
        </div>
        <div class="head-actions">
          <UIButton v-if="!isRunning && inst.status !== 'creating'" :loading="acting === 'start'" @click="act('start')">启动</UIButton>
          <template v-else-if="isRunning">
            <UIButton type="ghost" :loading="acting === 'restart'" @click="act('restart')">重启</UIButton>
            <UIButton type="ghost" :loading="acting === 'stop'" @click="act('stop')">停止</UIButton>
          </template>
          <UIButton type="text" class="danger" @click="delOpen = true">删除</UIButton>
        </div>
      </div>

      <div class="tabs-row">
        <UITabs v-model="tab" :tabs="tabs" />
      </div>

      <Transition name="fade-slide" mode="out-in">
        <OverviewTab v-if="tab === 'overview'" :key="'o'" :instance="inst" @changed="load" />
        <FileExplorer v-else-if="tab === 'files'" :key="'f'" :instance-id="instanceId" />
        <DepsTab v-else-if="tab === 'deps'" :key="'d'" :instance="inst" />
        <LogsTab v-else :key="'l'" :instance="inst" />
      </Transition>

      <ConfirmDialog
        v-model:open="delOpen"
        title="删除实例"
        :message="`确定删除实例「${inst.name}」吗？容器将被删除并移除，宿主机目录中的文件会保留。`"
        confirm-text="删除"
        danger
        @confirm="doDelete"
      />
    </template>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.head-left { display: flex; align-items: center; gap: 12px; }
.back {
  display: inline-flex; align-items: center; gap: 5px;
  border: none; background: transparent; cursor: pointer;
  color: var(--text-dim); font-size: 13px; font-family: inherit; padding: 4px 8px;
  border-radius: 6px; transition: all .15s ease;
}
.back:hover { color: var(--primary-strong); background: var(--primary-soft); }
.back-icon {
  width: 8px; height: 8px;
  border-left: 1.6px solid currentColor; border-bottom: 1.6px solid currentColor;
  transform: rotate(45deg);
}
.port-chip {
  background: var(--primary-soft); color: var(--primary-deep);
  padding: 3px 10px; border-radius: 8px; font-size: 13px; font-weight: 700;
}
.head-actions { display: flex; align-items: center; gap: 8px; }
.tabs-row { margin: 18px 0 16px; }
.loading { padding: 60px; text-align: center; }
</style>
