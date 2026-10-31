<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { startTask } from '../api/tasks'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import OverviewTab from './instance/OverviewTab.vue'

const route = useRoute()
const router = useRouter()
const instanceId = computed(() => String(route.params.id))

const inst = ref(null)
const loading = ref(true)
const delOpen = ref(false)
const resetOpen = ref(false)
const resetBusy = ref(false)

// 面板内部侧栏：我的实例列表
const instances = ref([])

async function load(silent = false) {
  try {
    const { data } = await api.get(`/instances/${instanceId.value}`)
    inst.value = data
  } catch (e) {
    if (!silent) toastErr(errText(e))
    if (e.response?.status === 404) router.replace('/instances')
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

// 一键重置：清空全部文件、恢复到刚创建的样子（规格/端口/面板令牌不变）
async function doReset() {
  resetOpen.value = false
  resetBusy.value = true
  try {
    await api.post(`/instances/${instanceId.value}/reset`)
    toastOk('已重置：数据目录已清空，实例恢复到刚创建的样子')
    await load(); loadList()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    resetBusy.value = false
  }
}

async function doDelete() {
  try {
    const tid = startTask()  // 回收实例全程终端日志
    const { data } = await api.delete(`/instances/${instanceId.value}`,
      { headers: { 'X-Task-Id': tid } })
    toastOk(data.detail || '已删除')
    router.push('/instances')
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
  load()
})

// ---------- 售卖生命周期：暂停横幅 / 续费 / 升级 ----------
const SUSPEND_WHY = { expire: '已到期', traffic: '流量超限', disk: '磁盘超限' }
const suspended = computed(() => !!inst.value?.suspended_at)
const expireTs = computed(() => {
  const raw = inst.value?.expire_at
  return raw ? new Date(raw.endsWith('Z') ? raw : raw + 'Z').getTime() : 0
})
const expireSoon = computed(() => {
  if (!expireTs.value) return false
  const left = expireTs.value - Date.now()
  return left > 0 && left < 7 * 864e5
})
const expireLeftText = computed(() => {
  if (!expireTs.value) return ''
  const left = expireTs.value - Date.now()
  if (left <= 0) return '已到期'
  const days = left / 864e5
  return days >= 1 ? `${Math.ceil(days)} 天后到期` : `${Math.max(1, Math.round(left / 36e5))} 小时后到期`
})

const plans = ref([])
const renewOpen = ref(false)
const renewPlan = ref(null)
const renewCoupon = ref('')
const renewBusy = ref(false)
const upgOpen = ref(false)
const upgPlan = ref(null)
const upgBusy = ref(false)
const upgItems = ref([])   // 可升配套餐 + 补差价

const fmtMoney = c => `¥${(c / 100).toFixed(2)}`

async function ensurePlans() {
  if (plans.value.length) return
  try { plans.value = (await api.get('/plans')).data.filter(p => p.enabled !== false) } catch { plans.value = [] }
}

async function openRenew() {
  renewPlan.value = null
  renewCoupon.value = ''
  renewOpen.value = true
  await ensurePlans()
}

async function doRenew() {
  if (!renewPlan.value) return
  renewBusy.value = true
  try {
    const { data } = await api.post('/shop/renew', {
      instance_uuid: instanceId.value, plan_id: renewPlan.value,
      coupon_code: renewCoupon.value.trim(),
    })
    toastOk(`${data.detail || '续费成功'}，新到期 ${String(data.expire_at).slice(0, 10)}${data.resumed ? '，实例已恢复运行' : ''}`)
    renewOpen.value = false
    await load(); loadList()
  } catch (e) { toastErr(errText(e)) } finally { renewBusy.value = false }
}

async function openUpg() {
  upgOpen.value = true
  upgPlan.value = null
  await ensurePlans()
  const cur = inst.value || {}
  const eligible = plans.value.filter(p =>
    p.cpu >= (cur.cpu_limit || 0) && p.mem >= (cur.mem_limit || 0) && p.disk >= (cur.disk_quota || 0) &&
    !(p.cpu === cur.cpu_limit && p.mem === cur.mem_limit && p.disk === cur.disk_quota))
  upgItems.value = eligible.map(p => ({ ...p, charge: null }))
  for (const item of upgItems.value) {
    try {
      const { data } = await api.get(`/instances/${instanceId.value}/upgrade/quote`, { params: { plan_id: item.id } })
      item.charge = data.charge_cents
    } catch { item.charge = null }
  }
}

async function doUpg() {
  if (!upgPlan.value) return
  upgBusy.value = true
  try {
    const { data } = await api.post(`/instances/${instanceId.value}/upgrade`, { plan_id: upgPlan.value })
    toastOk(`${data.detail || '升配成功'}${data.paid_cents ? `，补差价 ${fmtMoney(data.paid_cents)}` : '（无需补差价）'}`)
    upgOpen.value = false
    await load(); loadList()
  } catch (e) { toastErr(errText(e)) } finally { upgBusy.value = false }
}
</script>

<template>
  <div class="detail-layout">
    <!-- 左侧：我的实例列表 -->
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
    </aside>

    <!-- 右侧：内容区（文件管理/日志/依赖等在独立面板，主控只留关键操作） -->
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
            <UIButton type="ghost" @click="openUpg">升级</UIButton>
            <UIButton type="ghost" :loading="resetBusy" @click="resetOpen = true">重置</UIButton>
            <UIButton type="ghost" class="danger" @click="delOpen = true">删除</UIButton>
          </div>
        </div>

        <!-- 平台暂停 / 到期提醒横幅 -->
        <div v-if="suspended" class="life-banner danger">
          <span>⛔ 实例已被平台暂停（{{ SUSPEND_WHY[inst.suspend_reason] || inst.suspend_reason || '平台策略' }}），数据已保留。
            <template v-if="inst.suspend_reason === 'expire'">请续费恢复运行。</template>
            <template v-else>请处理超限问题后联系管理员恢复。</template>
          </span>
          <UIButton v-if="inst.suspend_reason === 'expire'" size="sm" @click="openRenew">立即续费</UIButton>
        </div>
        <div v-else-if="expireSoon" class="life-banner warn">
          <span>⏳ 实例{{ expireLeftText }}（{{ inst.expire_at?.slice(0, 16).replace('T', ' ') }}），到期后将暂停，数据保留。</span>
          <UIButton size="sm" type="ghost" @click="openRenew">续费</UIButton>
        </div>

        <OverviewTab :key="'o' + instanceId" :instance="inst" @changed="load" />

        <ConfirmDialog
          v-model:open="delOpen"
          title="删除实例"
          :message="`确定删除实例「${inst.name}」吗？容器将被删除并移除，宿主机目录中的文件会保留。`"
          confirm-text="删除"
          danger
          @confirm="doDelete"
        />

        <ConfirmDialog
          v-model:open="resetOpen"
          title="一键重置"
          :message="`确定把实例「${inst.name}」重置到刚创建的样子吗？实例内的全部文件将被清空且无法恢复（规格、端口、面板密钥保持不变）。`"
          confirm-text="确认重置"
          danger
          @confirm="doReset"
        />

        <!-- 续费弹窗 -->
        <div v-if="renewOpen" class="qmodal-mask" @click.self="renewOpen = false">
          <div class="qmodal-card">
            <div class="qmodal-head">
              <span>续费 · {{ inst.name }}</span>
              <button class="qmodal-x" @click="renewOpen = false">✕</button>
            </div>
            <div class="qmodal-body">
              <div class="plan-grid">
                <button v-for="p in plans" :key="p.id" class="plan-card"
                        :class="{ on: renewPlan === p.id }" @click="renewPlan = p.id">
                  <span class="plan-name">{{ p.name }}</span>
                  <span class="plan-price">{{ fmtMoney(p.price_cents) }}<i>/{{ p.days }}天</i></span>
                  <span class="plan-spec">{{ p.cpu }}核 · {{ p.mem }}MB · {{ p.disk / 1024 }}GB</span>
                </button>
                <div v-if="!plans.length" class="plan-empty">暂无可续费套餐</div>
              </div>
              <input v-model="renewCoupon" class="coupon-input" placeholder="优惠券码（可选）">
              <p class="plan-note">按所选套餐价格从当前到期时间顺延；等级折扣与优惠券在结算时生效。到期暂停的实例续费后自动恢复。</p>
            </div>
            <div class="qmodal-foot">
              <UIButton type="ghost" @click="renewOpen = false">取消</UIButton>
              <UIButton :disabled="!renewPlan" :loading="renewBusy" @click="doRenew">确认续费</UIButton>
            </div>
          </div>
        </div>

        <!-- 升配弹窗 -->
        <div v-if="upgOpen" class="qmodal-mask" @click.self="upgOpen = false">
          <div class="qmodal-card">
            <div class="qmodal-head">
              <span>升级 · {{ inst.name }}</span>
              <button class="qmodal-x" @click="upgOpen = false">✕</button>
            </div>
            <div class="qmodal-body">
              <div class="plan-grid">
                <button v-for="p in upgItems" :key="p.id" class="plan-card"
                        :class="{ on: upgPlan === p.id }" @click="upgPlan = p.id">
                  <span class="plan-name">{{ p.name }}</span>
                  <span class="plan-price">{{ p.charge === null ? '询价中' : (p.charge ? `补差 ${fmtMoney(p.charge)}` : '免费')}}</span>
                  <span class="plan-spec">{{ p.cpu }}核 · {{ p.mem }}MB · {{ p.disk / 1024 }}GB</span>
                </button>
                <div v-if="!upgItems.length" class="plan-empty">暂无更高规格套餐（升配需目标规格不低于当前）</div>
              </div>
              <p class="plan-note">按剩余天数补差价（余额扣款），立即重建容器生效，到期时间不变。</p>
            </div>
            <div class="qmodal-foot">
              <UIButton type="ghost" @click="upgOpen = false">取消</UIButton>
              <UIButton :disabled="!upgPlan" :loading="upgBusy" @click="doUpg">确认升级</UIButton>
            </div>
          </div>
        </div>
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

.detail-main { flex: 1; min-width: 0; }
.head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.head-left { display: flex; align-items: center; gap: 12px; }
.port-chip {
  background: var(--primary-soft); color: var(--primary-deep);
  padding: 3px 10px; border-radius: 8px; font-size: 13px; font-weight: 700;
}
.head-actions { display: flex; align-items: center; gap: 8px; }

.loading { padding: 40px 0; text-align: center; }

/* 暂停 / 到期横幅 */
.life-banner {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-top: 12px; padding: 10px 14px; border-radius: 10px; font-size: 13px;
}
.life-banner.danger {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 35%, transparent);
  color: var(--danger);
}
.life-banner.warn {
  background: color-mix(in srgb, var(--warn, #d97706) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--warn, #d97706) 35%, transparent);
  color: var(--warn, #b45309);
}

/* 续费 / 升配弹窗 */
.qmodal-mask {
  position: fixed; inset: 0; z-index: 80; background: rgba(0,0,0,.42);
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.qmodal-card {
  width: 560px; max-width: 100%; max-height: 86vh; overflow-y: auto;
  background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
  box-shadow: var(--shadow-lg);
}
.qmodal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 13px 16px; border-bottom: 1px solid var(--line);
  font-size: 14px; font-weight: 700; color: var(--text);
}
.qmodal-x { border: none; background: transparent; cursor: pointer; color: var(--text-dim); font-size: 13px; }
.qmodal-body { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.plan-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.plan-card {
  display: flex; flex-direction: column; gap: 3px; padding: 10px 12px; text-align: left;
  border: 1.5px solid var(--line); border-radius: 10px; background: transparent;
  cursor: pointer; font-family: inherit; transition: all .15s ease;
}
.plan-card:hover { border-color: var(--primary); }
.plan-card.on { border-color: var(--primary); background: var(--primary-soft); }
.plan-name { font-size: 12.5px; font-weight: 700; color: var(--text); }
.plan-price { font-size: 15px; font-weight: 800; color: var(--primary-strong); }
.plan-price i { font-style: normal; font-size: 10.5px; color: var(--text-dim); font-weight: 500; }
.plan-spec { font-size: 11px; color: var(--text-2); }
.plan-empty { grid-column: 1 / -1; text-align: center; color: var(--text-dim); font-size: 12.5px; padding: 14px 0; }
.coupon-input {
  height: 34px; padding: 0 10px; border: 1px solid var(--line); border-radius: 9px;
  background: transparent; color: var(--text); font-family: inherit; font-size: 12.5px; outline: none;
}
.coupon-input:focus { border-color: var(--primary); }
.plan-note { font-size: 11.5px; color: var(--text-dim); line-height: 1.6; margin: 0; }
.qmodal-foot {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 12px 16px; border-top: 1px solid var(--line);
}

@media (max-width: 960px) {
  .detail-layout { flex-direction: column; }
  .detail-side { width: 100%; position: static; flex-direction: row; flex-wrap: wrap; }
  .side-group { flex: 1; min-width: 160px; }
}
</style>
