<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { startTask } from '../api/tasks'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITag from '../components/ui/UITag.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'

const instances = ref([])
const loading = ref(true)
const delOpen = ref(false)
const delTarget = ref(null)

async function load() {
  try {
    const { data } = await api.get('/instances')
    instances.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}

async function togglePower(inst) {
  const action = inst.status === 'running' ? 'stop' : 'start'
  try {
    await api.post(`/instances/${inst.id}/${action}`)
    toastOk(action === 'start' ? '已启动' : '已停止')
    load()
  } catch (e) {
    toastErr(errText(e))
  }
}

const openingId = ref('')

// 点击卡片直接进入独立面板（新标签页，一键登录）
async function openPanel(inst) {
  if (openingId.value) return
  openingId.value = inst.id
  try {
    const { data } = await api.post(`/instances/${inst.id}/panel-token`)
    window.open(`${data.panel_url}?t=${data.panel_token}`, '_blank')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    openingId.value = ''
  }
}

async function doDelete() {
  try {
    const tid = startTask()  // 回收实例全程终端日志
    const { data } = await api.delete(`/instances/${delTarget.value.id}`,
      { headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '已删除')
    delOpen.value = false
    load()
  } catch (e) {
    toastErr(errText(e))
  }
}

function expireInfo(inst) {
  if (!inst.expire_at) return null
  const days = Math.ceil((new Date(inst.expire_at).getTime() - Date.now()) / 86400000)
  if (days < 0) return { tone: 'danger', text: '已过期' }
  if (days <= 3) return { tone: 'danger', text: `剩 ${days} 天` }
  if (days <= 7) return { tone: 'warn', text: `剩 ${days} 天` }
  return { tone: 'ok', text: `剩 ${days} 天` }
}

let timer
onMounted(() => {
  load()
  timer = setInterval(load, 8000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">我的实例</h1>
        <p class="page-sub">每个实例 = 独立容器 + 挂载目录 + 外部端口，可在商城购买开通</p>
      </div>
    </div>

    <EmptyState v-if="!loading && !instances.length" text="还没有实例，到商城选购套餐即可开通" />

    <TransitionGroup v-else name="fade" tag="div" class="grid">
      <div v-for="inst in instances" :key="inst.id" class="inst-card card" @click="openPanel(inst)">
        <div class="head">
          <span class="name">{{ inst.name }}</span>
          <StatusDot :status="inst.status" />
        </div>
        <div class="tags">
          <UITag>{{ inst.image }}</UITag>
          <UITag tone="dim">CPU {{ inst.cpu_limit }} 核</UITag>
          <UITag tone="dim">内存 {{ inst.mem_limit }}MB</UITag>
        </div>
        <div class="info">
          <div class="info-row">
            <span class="k">外部端口</span>
            <span class="v mono port">{{ inst.ext_port }}</span>
          </div>
          <div class="info-row">
            <span class="k">到期时间</span>
            <span class="v expire-cell">
              <template v-if="inst.expire_at">
                {{ new Date(inst.expire_at).toLocaleDateString() }}
                <UITag v-if="expireInfo(inst)" :tone="expireInfo(inst).tone" class="mini-tag">{{ expireInfo(inst).text }}</UITag>
              </template>
              <template v-else>长期有效</template>
            </span>
          </div>
        </div>
        <div class="foot" @click.stop>
          <UIButton v-if="inst.status === 'running'" type="ghost" @click="togglePower(inst)">停止</UIButton>
          <UIButton v-else :disabled="inst.status === 'creating'" @click="togglePower(inst)">启动</UIButton>
          <UIButton type="text" class="danger" @click="delTarget = inst; delOpen = true">删除</UIButton>
        </div>
      </div>
    </TransitionGroup>

    <ConfirmDialog
      v-model:open="delOpen"
      title="删除实例"
      :message="`确定删除实例「${delTarget?.name}」吗？容器将被删除并移除，宿主机目录中的文件会保留。`"
      confirm-text="删除"
      danger
      @confirm="doDelete"
    />
  </div>
</template>

<style scoped>
.page-head {
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 22px;
}
.grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px;
}
.inst-card {
  padding: 18px; cursor: pointer; display: flex; flex-direction: column; gap: 12px;
  transition: all .18s ease;
}
.inst-card:hover {
  transform: translateY(-3px); box-shadow: var(--shadow-lg);
  border-color: var(--primary);
}
.head { display: flex; align-items: center; justify-content: space-between; }
.name { font-size: 15px; font-weight: 700; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; }
.info { display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.info-row { display: flex; justify-content: space-between; }
.k { color: var(--text-dim); }
.v { color: var(--text-2); }
.port { color: var(--primary-strong); font-weight: 700; font-size: 14px; }
.expire-cell { display: inline-flex; align-items: center; gap: 6px; }
.mini-tag { transform: scale(.88); }
.foot { display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--line); padding-top: 12px; }
</style>
