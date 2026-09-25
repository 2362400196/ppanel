<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { startTask } from '../api/tasks'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import FileExplorer from '../components/FileExplorer.vue'
import OverviewTab from './instance/OverviewTab.vue'
import UsageTab from './instance/UsageTab.vue'
import DomainTab from './instance/DomainTab.vue'
import OpsTab from './instance/OpsTab.vue'
import DepsTab from './instance/DepsTab.vue'
import LogsTab from './instance/LogsTab.vue'

const route = useRoute()
const router = useRouter()
const instanceId = computed(() => String(route.params.id))

const inst = ref(null)
const tab = ref('overview')
const loading = ref(true)
const delOpen = ref(false)
const acting = ref('')

// 面板内部侧栏：我的实例列表
const instances = ref([])

const tabs = [
  { key: 'overview', label: '概览' },
  { key: 'usage', label: '用量统计' },
  { key: 'files', label: '文件管理' },
  { key: 'domain', label: '域名绑定' },
  { key: 'deps', label: '依赖' },
  { key: 'logs', label: '日志' },
  { key: 'ops', label: '操作记录' }
]

const curTab = computed(() => tabs.find(t => t.key === tab.value)?.label || '')

async function load(silent = false) {
  try {
    const { data } = await api.get(`/instances/${instanceId.value}`)
    const first = !inst.value
    inst.value = data
    if (first) startCmdDraft.value = data.start_cmd
  } catch (e) {
    if (!silent) toastErr(errText(e))
    if (e.response?.status === 404) router.replace('/')
  } finally {
    loading.value = false
  }
}

async function loadList(silent = true) {
  try {
    const { data } = await api.get('/instances')
    instances.value = data
  } catch { if (!silent) toastErr(errText(new Error('实例列表加载失败'))) }
}

const startCmdDraft = ref('')

async function act(action) {
  acting.value = action
  try {
    await api.post(`/instances/${instanceId.value}/${action}`)
    toastOk({ start: '已启动', stop: '已停止', restart: '已重启' }[action] || '操作成功')
    await load()
    loadList()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    acting.value = ''
  }
}

async function doDelete() {
  try {
    const tid = startTask()  // 回收实例全程终端日志
    const { data } = await api.delete(`/instances/${instanceId.value}`,
      { headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '已删除')
    router.push('/')
  } catch (e) {
    toastErr(errText(e))
  }
}

function switchInst(id) {
  if (id !== instanceId.value) router.push(`/instances/${id}`)
}

let timer
onMounted(() => {
  load()
  loadList()
  timer = setInterval(() => { load(true); loadList() }, 5000)
})
onUnmounted(() => clearInterval(timer))

watch(instanceId, () => {
  loading.value = true
  inst.value = null
  tab.value = 'overview'
  load()
})

const isRunning = computed(() => inst.value?.status === 'running')
</script>

<template>
  <div class="detail-layout">
    <!-- 左侧：我的实例 + 功能导航 -->
    <aside class="detail-side">
      <div class="side-group">
        <div class="group-title">我的实例</div>
        <div class="inst-list">
          <button
            v-for="i in instances" :key="i.id"
            class="inst-item" :class="{ active: i.id === instanceId }"
            @click="switchInst(i.id)"
          >
            <StatusDot :status="i.status" />
            <span class="inst-name">{{ i.name }}</span>
          </button>
          <div v-if="!instances.length" class="inst-empty text-dim">暂无实例</div>
        </div>
      </div>

      <div class="side-group">
        <div class="group-title">功能</div>
        <div class="func-list">
          <button
            v-for="t in tabs" :key="t.key"
            class="func-item" :class="{ active: tab === t.key }"
            @click="tab = t.key"
          >
            {{ t.label }}
          </button>
        </div>
      </div>

      <div class="side-danger">
        <UIButton type="text" class="danger" @click="delOpen = true">删除实例</UIButton>
      </div>
    </aside>

    <!-- 右侧：内容区 -->
    <section class="detail-main">
      <div v-if="loading" class="loading text-dim">加载中…</div>
      <template v-else-if="inst">
        <div class="head">
          <div class="head-left">
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
          </div>
        </div>

        <div class="tab-indicator">
          <Transition name="fade-slide" mode="out-in">
            <span :key="curTab" class="tab-label">{{ curTab }}</span>
          </Transition>
        </div>

        <Transition name="fade-slide" mode="out-in">
          <OverviewTab v-if="tab === 'overview'" :key="'o' + instanceId" :instance="inst" @changed="load" />
          <UsageTab v-else-if="tab === 'usage'" :key="'u' + instanceId" :instance="inst" />
          <FileExplorer v-else-if="tab === 'files'" :key="'f' + instanceId" :instance-id="instanceId" />
          <DomainTab v-else-if="tab === 'domain'" :key="'d' + instanceId" :instance="inst" />
          <DepsTab v-else-if="tab === 'deps'" :key="'dp' + instanceId" :instance="inst" />
          <LogsTab v-else-if="tab === 'logs'" :key="'l' + instanceId" :instance="inst" />
          <OpsTab v-else :key="'op' + instanceId" :instance="inst" />
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
    </section>
  </div>
</template>

<style scoped>
.detail-layout { display: flex; gap: 22px; align-items: flex-start; }

.detail-side {
  width: 190px; flex-shrink: 0; position: sticky; top: 0;
  display: flex; flex-direction: column; gap: 18px;
}
.side-group { display: flex; flex-direction: column; gap: 6px; }
.group-title {
  font-size: 11px; font-weight: 700; color: var(--text-dim);
  letter-spacing: 1px; padding: 0 10px; margin-bottom: 2px;
}
.inst-list, .func-list { display: flex; flex-direction: column; gap: 2px; }

.inst-item {
  display: flex; align-items: center; gap: 8px; height: 34px; padding: 0 10px;
  border: none; background: transparent; cursor: pointer; font-family: inherit;
  border-radius: 8px; font-size: 13px; color: var(--text-2);
  transition: all .15s ease; text-align: left; position: relative;
}
.inst-item:hover { background: var(--primary-soft); color: var(--text); }
.inst-item.active {
  background: var(--primary-soft); color: var(--primary-deep); font-weight: 600;
  animation: breathe 2.4s ease-in-out infinite;
}
.inst-item.active::before {
  content: ''; position: absolute; left: 0; top: 8px; bottom: 8px; width: 3px;
  border-radius: 2px; background: var(--primary);
}
.inst-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@keyframes breathe { 50% { box-shadow: 0 0 0 3px rgba(47,181,159,.08); } }

.inst-empty { font-size: 12px; padding: 6px 10px; }

.func-item {
  display: flex; align-items: center; height: 34px; padding: 0 10px;
  border: none; background: transparent; cursor: pointer; font-family: inherit;
  border-radius: 8px; font-size: 13px; color: var(--text-2);
  transition: all .15s ease; position: relative; text-align: left;
}
.func-item:hover { background: var(--primary-soft); color: var(--text); }
.func-item.active {
  background: var(--primary-soft); color: var(--primary-deep); font-weight: 600;
  animation: breathe 2.4s ease-in-out infinite;
}
.func-item.active::before {
  content: ''; position: absolute; left: 0; top: 8px; bottom: 8px; width: 3px;
  border-radius: 2px; background: var(--primary);
}

.side-danger { border-top: 1px solid var(--line); padding-top: 10px; }

.detail-main { flex: 1; min-width: 0; }
.head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.head-left { display: flex; align-items: center; gap: 12px; }
.port-chip {
  background: var(--primary-soft); color: var(--primary-deep);
  padding: 3px 10px; border-radius: 8px; font-size: 13px; font-weight: 700;
}
.head-actions { display: flex; align-items: center; gap: 8px; }

.tab-indicator { margin: 14px 0 12px; }
.tab-label { display: inline-block; font-size: 12px; color: var(--text-dim); font-weight: 600; }

.loading { padding: 40px 0; text-align: center; }

@media (max-width: 960px) {
  .detail-layout { flex-direction: column; }
  .detail-side { width: 100%; position: static; flex-direction: row; flex-wrap: wrap; }
  .side-group { flex: 1; min-width: 160px; }
}
</style>
