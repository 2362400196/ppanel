<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { STATUS_TONE } from '../utils/format'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITag from '../components/ui/UITag.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import CreateInstanceModal from '../components/CreateInstanceModal.vue'

const router = useRouter()
const instances = ref([])
const loading = ref(true)
const createOpen = ref(false)
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

function onCreated() {
  load()
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

async function doDelete() {
  try {
    const { data } = await api.delete(`/instances/${delTarget.value.id}`)
    toastOk(data.detail || '已删除')
    delOpen.value = false
    load()
  } catch (e) {
    toastErr(errText(e))
  }
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
        <p class="page-sub">每个实例 = 独立容器 + 挂载目录 + 外部端口</p>
      </div>
      <UIButton size="md" @click="createOpen = true">创建实例</UIButton>
    </div>

    <EmptyState v-if="!loading && !instances.length" text="还没有实例，点击右上角创建第一个 Python 运行环境" />

    <TransitionGroup v-else name="fade" tag="div" class="grid">
      <div v-for="inst in instances" :key="inst.id" class="inst-card card" @click="router.push(`/instances/${inst.id}`)">
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
            <span class="v">{{ inst.expire_at ? '到期预留' : '长期有效' }}</span>
          </div>
        </div>
        <div class="foot" @click.stop>
          <UIButton v-if="inst.status === 'running'" type="ghost" @click="togglePower(inst)">停止</UIButton>
          <UIButton v-else :disabled="inst.status === 'creating'" @click="togglePower(inst)">启动</UIButton>
          <UIButton type="text" class="danger" @click="delTarget = inst; delOpen = true">删除</UIButton>
        </div>
      </div>
    </TransitionGroup>

    <CreateInstanceModal v-model:open="createOpen" @created="onCreated" />
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
.foot { display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--line); padding-top: 12px; }
</style>
