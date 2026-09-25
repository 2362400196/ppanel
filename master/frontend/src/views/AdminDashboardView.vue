<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr } from '../components/ui/toast'
import UITag from '../components/ui/UITag.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import UITable from '../components/ui/UITable.vue'

const loading = ref(true)
const users = ref([])
const instances = ref([])
const nodes = ref([])

const runningCount = computed(() => instances.value.filter(i => i.status === 'running').length)
const onlineNodes = computed(() => nodes.value.filter(n => n.online).length)
const statusDist = computed(() => {
  const c = { running: 0, stopped: 0, exited: 0, creating: 0 }
  instances.value.forEach(i => { c[i.status] = (c[i.status] || 0) + 1 })
  return c
})
const distTotal = computed(() => instances.value.length || 1)
const distSegs = computed(() => [
  { key: 'running', label: '运行中', color: '#2fb59f', n: statusDist.value.running },
  { key: 'stopped', label: '已停止', color: '#8a94a0', n: statusDist.value.stopped },
  { key: 'exited', label: '异常退出', color: '#e5484d', n: statusDist.value.exited },
  { key: 'creating', label: '创建中', color: '#f59e0b', n: statusDist.value.creating }
].filter(s => s.n > 0))

const recent = computed(() => instances.value.slice(0, 6))
const recentCols = [
  { key: 'name', label: '实例' },
  { key: 'owner', label: '归属用户', width: '120px' },
  { key: 'image', label: '镜像', width: '160px' },
  { key: 'status', label: '状态', width: '110px' },
  { key: 'created_at', label: '创建时间', width: '160px' }
]

const cards = computed(() => [
  { label: '用户总数', value: users.value.length, sub: `管理员 ${users.value.filter(u => u.role === 'admin').length} · 普通用户 ${users.value.filter(u => u.role !== 'admin').length}`, to: '/users', icon: 'users' },
  { label: '实例总数', value: instances.value.length, sub: `${runningCount.value} 个运行中`, to: '/manage', icon: 'server' },
  { label: '在线节点', value: `${onlineNodes.value} / ${nodes.value.length}`, sub: nodes.value.length ? '节点正常接入' : '尚未接入节点', to: '/nodes', icon: 'nodes' },
  { label: '运行率', value: instances.value.length ? Math.round(runningCount.value / instances.value.length * 100) + '%' : '—', sub: '运行实例 / 总实例', to: '/manage', icon: 'pulse' }
])

function fmtWhen(v) {
  const d = new Date(v)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

onMounted(async () => {
  try {
    const [u, i, n] = await Promise.all([
      api.get('/admin/users'),
      api.get('/instances', { params: { all: 1 } }),
      api.get('/admin/nodes')
    ])
    users.value = u.data
    instances.value = i.data
    nodes.value = n.data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">仪表盘</h1>
        <p class="page-sub">平台资源与运行状况总览</p>
      </div>
    </div>

    <div class="stat-grid">
      <router-link v-for="c in cards" :key="c.label" :to="c.to" class="stat-card">
        <div class="stat-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <template v-if="c.icon === 'users'"><path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2" /><circle cx="10" cy="7" r="4" /><path d="M21 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></template>
          <template v-else-if="c.icon === 'server'"><rect x="2" y="3" width="20" height="7" rx="2" /><rect x="2" y="14" width="20" height="7" rx="2" /><line x1="6" y1="6.5" x2="6.01" y2="6.5" /><line x1="6" y1="17.5" x2="6.01" y2="17.5" /></template>
          <template v-else-if="c.icon === 'nodes'"><circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" /><line x1="8.6" y1="10.5" x2="15.4" y2="6.5" /><line x1="8.6" y1="13.5" x2="15.4" y2="17.5" /></template>
          <template v-else><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></template>
        </svg></div>
        <div class="stat-meta">
          <span class="stat-value">{{ c.value }}</span>
          <span class="stat-label">{{ c.label }}</span>
          <span class="stat-sub">{{ c.sub }}</span>
        </div>
      </router-link>
    </div>

    <div class="dash-grid">
      <div class="dash-card">
        <div class="dash-card-head">
          <span class="dash-card-title">实例状态分布</span>
          <span class="text-dim">共 {{ instances.length }} 个</span>
        </div>
        <div v-if="distSegs.length" class="dist-bar">
          <div v-for="s in distSegs" :key="s.key" :style="{ width: (s.n / distTotal * 100) + '%', background: s.color }"
               :title="`${s.label} ${s.n}`" />
        </div>
        <div v-else class="dist-empty">暂无实例，去「商品管理」创建商品后即可开通。</div>
        <div class="dist-legend">
          <span v-for="s in distSegs" :key="s.key" class="dist-item">
            <i :style="{ background: s.color }" />{{ s.label }} <b>{{ s.n }}</b>
          </span>
        </div>
      </div>

      <div class="dash-card">
        <div class="dash-card-head">
          <span class="dash-card-title">节点状况</span>
          <router-link to="/nodes" class="dash-more">管理 →</router-link>
        </div>
        <div v-if="nodes.length" class="node-list">
          <div v-for="n in nodes" :key="n.id" class="node-row">
            <StatusDot :status="n.online ? 'running' : 'exited'" />
            <span class="node-name">{{ n.name }}</span>
            <UITag :tone="n.online ? 'primary' : 'danger'">{{ n.online ? '在线' : '离线' }}</UITag>
          </div>
        </div>
        <div v-else class="dist-empty">还没有接入被控节点，到「节点管理」添加。</div>
      </div>
    </div>

    <div class="dash-card">
      <div class="dash-card-head">
        <span class="dash-card-title">最近创建的实例</span>
        <router-link to="/manage" class="dash-more">全部实例 →</router-link>
      </div>
      <UITable :columns="recentCols" :rows="recent" :loading="loading">
        <template #col-status="{ row }"><StatusDot :status="row.status" /></template>
        <template #col-created_at="{ row }">{{ fmtWhen(row.created_at) }}</template>
      </UITable>
    </div>
  </div>
</template>

<style scoped>
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
.stat-card {
  display: flex; align-items: center; gap: 14px; padding: 18px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
  text-decoration: none; color: inherit; transition: all .18s ease;
}
.stat-card:hover { border-color: var(--primary); transform: translateY(-2px); box-shadow: 0 8px 24px rgba(15, 40, 34, .08); }
.stat-icon {
  width: 46px; height: 46px; border-radius: 12px; flex-shrink: 0;
  background: var(--primary-soft); color: var(--primary-deep);
  display: flex; align-items: center; justify-content: center;
}
.stat-icon svg { width: 22px; height: 22px; }
.stat-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.stat-value { font-size: 22px; font-weight: 800; font-family: var(--font-mono); }
.stat-label { font-size: 13px; font-weight: 600; }
.stat-sub { font-size: 12px; color: var(--text-dim); }

.dash-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
.dash-card {
  margin-top: 0; background: var(--panel); border: 1px solid var(--line);
  border-radius: 14px; padding: 18px; margin-bottom: 14px;
}
.dash-grid .dash-card { margin-bottom: 0; }
.dash-card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.dash-card-title { font-size: 14px; font-weight: 700; }
.dash-more { font-size: 12.5px; color: var(--primary-deep); text-decoration: none; font-weight: 600; }
.dash-more:hover { text-decoration: underline; }

.dist-bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden; background: var(--bg); }
.dist-bar div { transition: width .4s ease; }
.dist-empty { font-size: 13px; color: var(--text-dim); padding: 18px 0; text-align: center; }
.dist-legend { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 12px; }
.dist-item { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--text-dim); }
.dist-item b { color: var(--text); font-family: var(--font-mono); }
.dist-item i { width: 9px; height: 9px; border-radius: 3px; }

.node-list { display: flex; flex-direction: column; gap: 10px; }
.node-row { display: flex; align-items: center; gap: 10px; }
.node-name { font-weight: 600; font-size: 13.5px; flex: 1; }

@media (max-width: 900px) { .dash-grid { grid-template-columns: 1fr; } }
</style>
