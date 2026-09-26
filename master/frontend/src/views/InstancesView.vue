<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { startTask } from '../api/tasks'
import { toastErr, toastOk } from '../components/ui/toast'
import { useAuthStore } from '../stores/auth'
import UIButton from '../components/ui/UIButton.vue'
import UITag from '../components/ui/UITag.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'

const auth = useAuthStore()
const instances = ref([])
const loading = ref(true)
const delOpen = ref(false)
const delTarget = ref(null)

// 仪表盘数据：钱包 / 福利 / 流水
const wallet = ref(null)
const rewards = ref(null)
const records = ref([])

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

async function loadDashboard() {
  try {
    const [w, r, rec] = await Promise.all([
      api.get('/wallet'), api.get('/rewards/overview'), api.get('/wallet/records'),
    ])
    wallet.value = w.data
    rewards.value = r.data
    records.value = rec.data
  } catch { /* 仪表盘数据静默降级 */ }
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

// ---------- 仪表盘 ----------
const hour = new Date().getHours()
const greet = hour < 6 ? '夜深了' : hour < 12 ? '早上好' : hour < 14 ? '中午好' : hour < 18 ? '下午好' : '晚上好'
const dateLine = new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })

const runCount = computed(() => instances.value.filter(i => i.status === 'running').length)

const fmtYuan = (cents) => '¥' + ((cents || 0) / 100).toFixed(2)

// 本月消费：流水里 kind=shop 且已支付且本月
const monthSpend = computed(() => {
  const now = new Date(), y = now.getFullYear(), m = now.getMonth()
  let cents = 0
  for (const r of records.value) {
    if (r.kind !== 'shop' || r.status !== 'paid' || !r.created_at) continue
    const d = new Date(r.created_at)
    if (d.getFullYear() === y && d.getMonth() === m) cents += r.amount_cents
  }
  return cents
})

// 7 天内到期 / 已过期的实例（提醒条）
const expireWarn = computed(() =>
  instances.value.filter(i => {
    if (!i.expire_at) return false
    return (new Date(i.expire_at).getTime() - Date.now()) / 86400000 <= 7
  }).sort((a, b) => new Date(a.expire_at) - new Date(b.expire_at)),
)

const levelPct = computed(() => {
  const r = rewards.value
  if (!r) return 0
  if (!r.next_level_exp) return 100
  return Math.min(100, Math.round(r.level_exp / r.next_level_exp * 100))
})

const levelSub = computed(() => {
  const r = rewards.value
  if (!r) return ''
  if (!r.next_level_exp) return '已是最高等级'
  return `再消费 ${fmtYuan(r.next_level_exp - r.level_exp)} 升 LV${r.level + 1}`
})

const discountText = computed(() => {
  const pct = rewards.value?.discount_pct ?? 100
  return pct >= 100 ? '无折扣' : `${pct} 折`
})

const pointsSub = computed(() => {
  const r = rewards.value
  if (!r) return ''
  return r.checked_today ? '今日已签到' : `签到 +${r.checkin_points}/天`
})

async function doCheckin() {
  try {
    const { data } = await api.post('/rewards/checkin')
    toastOk(data.detail || '签到成功')
    if (rewards.value) {
      rewards.value.checked_today = true
      rewards.value.points = data.points
    }
  } catch (e) {
    toastErr(errText(e))
  }
}

let timer
onMounted(() => {
  load()
  loadDashboard()
  timer = setInterval(load, 8000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <!-- 顶栏：问候 + 签到 -->
    <div class="page-head">
      <div>
        <h1 class="page-title">{{ greet }}，{{ auth.user?.username || '用户' }}</h1>
        <p class="page-sub">{{ dateLine }}</p>
      </div>
      <UIButton
        :type="rewards?.checked_today ? 'ghost' : 'primary'"
        :disabled="rewards?.checked_today"
        @click="doCheckin"
      >
        {{ rewards?.checked_today ? '今日已签到' : `签到 +${rewards?.checkin_points ?? 10} 积分` }}
      </UIButton>
    </div>

    <!-- 统计卡片（与管理员仪表盘同款） -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="3" width="20" height="7" rx="2" /><rect x="2" y="14" width="20" height="7" rx="2" /><line x1="6" y1="6.5" x2="6.01" y2="6.5" /><line x1="6" y1="17.5" x2="6.01" y2="17.5" />
          </svg>
        </div>
        <div class="stat-meta">
          <span class="stat-value">{{ runCount }} / {{ instances.length }}</span>
          <span class="stat-label">运行实例</span>
          <span class="stat-sub">运行中 / 全部实例</span>
        </div>
      </div>

      <router-link to="/wallet" class="stat-card">
        <div class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12V7H5a2 2 0 0 1 0-4h14v4" /><path d="M3 5v14a2 2 0 0 0 2 2h16v-5" /><path d="M18 12a2 2 0 0 0 0 4h4v-4Z" />
          </svg>
        </div>
        <div class="stat-meta">
          <span class="stat-value">{{ fmtYuan(wallet?.balance_cents) }}</span>
          <span class="stat-label">钱包余额</span>
          <span class="stat-sub link">去充值 →</span>
        </div>
      </router-link>

      <div class="stat-card">
        <div class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" /><polyline points="16 7 22 7 22 13" />
          </svg>
        </div>
        <div class="stat-meta">
          <span class="stat-value">{{ fmtYuan(monthSpend) }}</span>
          <span class="stat-label">本月消费</span>
          <span class="stat-sub">实付金额合计</span>
        </div>
      </div>

      <router-link to="/rewards" class="stat-card">
        <div class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
          </svg>
        </div>
        <div class="stat-meta">
          <span class="stat-value">{{ rewards?.points ?? '—' }}</span>
          <span class="stat-label">我的积分</span>
          <span class="stat-sub link">{{ pointsSub }} · LV{{ rewards?.level ?? 1 }} {{ discountText }}</span>
        </div>
      </router-link>

      <div class="stat-card">
        <div class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="8" r="6" /><path d="M15.477 12.89 17 22l-5-3-5 3 1.523-9.11" />
          </svg>
        </div>
        <div class="stat-meta">
          <span class="stat-value">LV{{ rewards?.level ?? 1 }}</span>
          <span class="stat-label">会员等级 · {{ discountText }}</span>
          <div class="bar"><i :style="{ width: levelPct + '%' }" /></div>
          <span class="stat-sub">{{ levelSub }}</span>
        </div>
      </div>
    </div>

    <!-- 到期提醒 -->
    <div v-if="expireWarn.length" class="warn-list">
      <div
        v-for="inst in expireWarn"
        :key="inst.id"
        class="warn-row"
        :class="expireInfo(inst)?.tone"
      >
        <b>{{ inst.name }}</b>
        <span>{{ expireInfo(inst)?.text }}{{ expireInfo(inst)?.text === '已过期' ? '，续期后可继续使用' : '到期，记得及时续期' }}</span>
        <router-link class="wlink" to="/shop">去续期</router-link>
      </div>
    </div>

    <!-- 实例卡片网格 -->
    <div class="sec-head">
      <h2>我的实例</h2>
      <span class="sec-sub">每个实例 = 独立容器 + 挂载目录 + 外部端口，点击卡片一键进入独立面板</span>
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
          <UIButton type="primary" :loading="openingId === inst.id" @click="openPanel(inst)">管理</UIButton>
          <div class="foot-right">
            <UIButton v-if="inst.status === 'running'" type="ghost" @click="togglePower(inst)">停止</UIButton>
            <UIButton v-else :disabled="inst.status === 'creating'" @click="togglePower(inst)">启动</UIButton>
            <UIButton type="text" class="danger" @click="delTarget = inst; delOpen = true">删除</UIButton>
          </div>
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
.page-title { font-size: 18px; font-weight: 700; margin: 0 0 4px; color: var(--text); }
.page-sub { margin: 0; font-size: 13px; color: var(--text-dim); }

/* ---------- 统计卡片（与管理员仪表盘同款） ---------- */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 22px; }
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
.stat-sub.link { color: var(--primary-strong); font-weight: 600; }
.bar {
  height: 4px; border-radius: 999px; background: var(--line);
  overflow: hidden; margin: 4px 0 2px;
}
.bar i {
  display: block; height: 100%; border-radius: 999px;
  background: var(--primary);
  transition: width .6s ease;
}

/* ---------- 到期提醒 ---------- */
.warn-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 20px; }
.warn-row {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; padding: 8px 14px; border-radius: 10px;
}
.warn-row.warn { background: var(--warn-soft); color: #9a6b13; }
.warn-row.danger { background: var(--danger-soft); color: #b91c1c; }
.warn-row b { margin-right: 4px; }
.wlink { margin-left: auto; font-weight: 700; text-decoration: none; border-bottom: 1px dashed currentColor; }
.wlink:hover { opacity: .75; }

/* ---------- 实例卡片网格 ---------- */
.sec-head {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 14px;
}
.sec-head h2 { font-size: 15px; font-weight: 700; margin: 0; color: var(--text); }
.sec-sub { font-size: 12px; color: var(--text-dim); }

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
.foot-right { display: flex; gap: 4px; align-items: center; }

.fade-enter-active, .fade-leave-active { transition: all .25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(6px); }
.fade-leave-active { position: absolute; }
</style>
