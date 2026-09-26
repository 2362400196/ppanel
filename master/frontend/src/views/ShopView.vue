<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UITag from '../components/ui/UITag.vue'
import UIModal from '../components/ui/UIModal.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import { startTask } from '../api/tasks'

const router = useRouter()
const buying = ref('')
const plans = ref([])
const loading = ref(true)

onMounted(async () => {
  try {
    const { data } = await api.get('/plans')
    plans.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
})

const priceText = p => `¥${(p.price_cents / 100).toFixed(p.price_cents % 100 ? 2 : 0)}`

// ---------- 商品详情弹窗 ----------
const detailOpen = ref(false)
const detail = ref(null)
const detailBuying = ref(false)

// 环境徽标：镜像前缀 → 简名
function envName(image) {
  const key = (image || 'python').split(':')[0].toLowerCase()
  const map = { python: 'Python', php: 'PHP', node: 'Node.js', golang: 'Golang', nginx: '静态站点', caddy: '静态站点' }
  return map[key] || key
}

function openDetail(p) {
  detail.value = p
  couponCode.value = ''
  couponCheck.value = null
  couponErr.value = ''
  detailOpen.value = true
}

async function buyDetail() {
  if (detailBuying.value) return
  detailBuying.value = true
  try {
    await buy(detail.value, couponCode.value)
    detailOpen.value = false
  } finally {
    detailBuying.value = false
  }
}

// 余额不足引导充值
const rcOpen = ref(false)

// 福利：等级折扣 + 优惠券
const rewards = ref(null)          // { level, discount_pct, points }
const couponCode = ref('')
const couponCheck = ref(null)      // 试算结果 { cut_cents, pay_cents }
const couponErr = ref('')
const checking = ref(false)

async function loadRewards() {
  try {
    const { data } = await api.get('/rewards/overview')
    rewards.value = data
  } catch { /* 福利信息失败不阻塞商城 */ }
}
loadRewards()

// 试算优惠券：校验可用性并显示到手价
async function checkCoupon() {
  const code = couponCode.value.trim().toUpperCase()
  if (!code || !detail.value || checking.value) return
  checking.value = true
  couponErr.value = ''
  couponCheck.value = null
  try {
    const { data } = await api.post('/rewards/coupon/check', { code, plan_id: detail.value.id })
    couponCheck.value = data
  } catch (e) {
    couponErr.value = errText(e)
  } finally {
    checking.value = false
  }
}

async function buy(plan, coupon = '') {
  buying.value = plan.id
  try {
    const { data: st } = await api.get('/pay/status')
    if (!st.enabled) { await directOpen(plan); return }  // 未启用支付：免费直接开通
    // 钱包扣款购买（等级折扣/券抵扣在后端完成）
    const { data } = await api.post('/shop/buy', { plan_id: plan.id, coupon_code: coupon })
    const saved = data.coupon_cut_cents ? `，券已抵 ¥${(data.coupon_cut_cents / 100).toFixed(2)}` : ''
    toastOk(`已开通「${data.name}」，实付 ¥${(data.paid_cents / 100).toFixed(2)}${saved}，+${data.points_earned} 积分`)
    router.push('/instances')
  } catch (e) {
    if (e?.response?.status === 402) rcOpen.value = true  // 余额不足 → 引导充值
    else toastErr(errText(e))
  } finally {
    buying.value = ''
  }
}

function goWallet() {
  rcOpen.value = false
  router.push('/wallet')
}

async function directOpen(plan) {
  const tid = startTask()  // 开通实例全程终端日志
  try {
    const suffix = Math.random().toString(36).slice(2, 6)
    const expire = new Date(Date.now() + plan.days * 86400000).toISOString()
    const { data } = await api.post('/instances', {
      name: `面板-${suffix}`,
      image: plan.image || 'python:3.11-slim',  // 环境由商品绑定，买家不可选
      cpu_limit: plan.cpu,
      mem_limit: plan.mem,
      disk_quota: plan.disk,
      expire_at: expire,
      traffic_gb: plan.traffic_gb || null,  // 月流量限额，0=不限
      node_id: plan.node_id || undefined,  // 商品绑定节点则在该节点开通
    }, { headers: { 'X-Task-Id': tid } })
    toastOk(`已开通「${data.name}」，有效期 ${plan.days} 天`)
    router.push('/instances')
  } finally {
    buying.value = ''
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">商城</h1>
        <p class="page-sub">选择套餐开通独立面板，运行环境由套餐决定，开通即用</p>
      </div>
    </div>

    <div v-if="!loading && !plans.length" class="empty">商品暂未上架，请稍后再来。</div>

    <div v-else class="plans">
      <div v-for="p in plans" :key="p.id" class="plan card">
        <div class="plan-top clickable" title="查看商品详情" @click="openDetail(p)">
          <UITag tone="primary">{{ p.name }}</UITag>
          <span class="mono price">{{ priceText(p) }}<i class="per">/ {{ p.days }} 天</i></span>
        </div>
        <p class="desc clickable" @click="openDetail(p)">{{ p.desc }}</p>
        <div class="specs">
          <div class="spec"><span class="k">环境</span><span class="v mono">{{ p.image || 'python:3.11-slim' }}</span></div>
          <div class="spec"><span class="k">CPU</span><span class="v">{{ p.cpu }} 核</span></div>
          <div class="spec"><span class="k">内存</span><span class="v">{{ p.mem }}MB</span></div>
          <div class="spec"><span class="k">磁盘</span><span class="v">{{ p.disk / 1024 }}GB</span></div>
          <div class="spec"><span class="k">月流量</span><span class="v">{{ p.traffic_gb ? p.traffic_gb + 'GB' : '不限' }}</span></div>
          <div class="spec"><span class="k">到期</span><span class="v">{{ p.days }} 天</span></div>
        </div>
        <UIButton class="buy" :loading="buying === p.id" @click="openDetail(p)">立即开通</UIButton>
        <button class="detail-link" @click="openDetail(p)">查看商品详情 ›</button>
      </div>
    </div>

    <p class="note text-dim">开通后可在仪表盘进入面板管理容器，到期前请续期。</p>

    <!-- 商品详情弹窗 -->
    <UIModal v-model:open="detailOpen" title="商品详情" width="480px">
      <div v-if="detail" class="dt">
        <div class="dt-head">
          <div class="env-badge mono">{{ envName(detail.image) }}</div>
          <div class="dt-title">
            <h3>{{ detail.name }}</h3>
            <p class="dt-price mono">{{ priceText(detail) }}<i class="per">/ {{ detail.days }} 天</i></p>
          </div>
        </div>
        <p v-if="detail.desc" class="dt-desc">{{ detail.desc }}</p>
        <div class="dt-specs">
          <div class="dt-row"><span>运行环境</span><b class="mono">{{ detail.image || 'python:3.11-slim' }}</b></div>
          <div class="dt-row"><span>CPU</span><b>{{ detail.cpu }} 核</b></div>
          <div class="dt-row"><span>内存</span><b>{{ detail.mem }} MB</b></div>
          <div class="dt-row"><span>磁盘空间</span><b>{{ detail.disk / 1024 }} GB</b></div>
          <div class="dt-row"><span>月流量</span><b>{{ detail.traffic_gb ? detail.traffic_gb + ' GB' : '不限' }}</b></div>
          <div class="dt-row"><span>有效期</span><b>{{ detail.days }} 天（到期前请续期）</b></div>
        </div>
        <div class="dt-perks">
          <span v-for="perk in ['独立管理面板', '自动 SSL 证书', '数据库备份恢复', '容器目录备份']" :key="perk" class="perk">
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 7" /></svg>
            {{ perk }}
          </span>
        </div>

        <!-- 等级折扣 -->
        <div v-if="rewards && rewards.discount_pct < 100" class="dt-discount">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 12l-8.5 8.5a2 2 0 0 1-2.8 0L3 14.8V3h11.8L20 8.2a2.7 2.7 0 0 1 0 3.8z" /><circle cx="7.5" cy="7.5" r="1.2" /></svg>
          LV{{ rewards.level }} 会员自动享 <b>{{ rewards.discount_pct }} 折</b>
        </div>
        <p v-else-if="rewards" class="dt-tip text-dim">累计消费升级会员等级，最高 88 折（福利中心可查看）</p>

        <!-- 优惠券 -->
        <div class="dt-coupon">
          <UIInput v-model="couponCode" placeholder="输入优惠券码（可选）" style="flex:1"
                   @keydown.enter="checkCoupon" />
          <UIButton type="ghost" :loading="checking" @click="checkCoupon">试算</UIButton>
        </div>
        <p v-if="couponCheck" class="dt-pay">
          <span class="text-dim">券已抵扣 ¥{{ (couponCheck.cut_cents / 100).toFixed(2) }}</span>
          <b class="mono">到手 ¥{{ (couponCheck.pay_cents / 100).toFixed(2) }}</b>
        </p>
        <p v-else-if="couponErr" class="dt-pay err">{{ couponErr }}</p>

        <p class="dt-tip text-dim">购买后从钱包余额扣款，消费每 1 元返 10 积分；开通即进入独立面板管理。</p>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="detailOpen = false">取消</UIButton>
        <UIButton :loading="detailBuying" @click="buyDetail">确认开通</UIButton>
      </template>
    </UIModal>

    <!-- 余额不足：引导去钱包充值 -->
    <ConfirmDialog v-model:open="rcOpen" title="余额不足"
                   message="钱包余额不够支付本商品，是否前往钱包充值？"
                   confirm-text="去充值" @confirm="goWallet" />
  </div>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 22px; }

.plans { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
.plan { padding: 20px; display: flex; flex-direction: column; gap: 12px; transition: all .18s ease; }
.plan:hover { transform: translateY(-3px); box-shadow: var(--shadow-lg); border-color: var(--primary); }
.plan-top { display: flex; align-items: center; justify-content: space-between; }
.price { font-size: 22px; font-weight: 800; color: var(--primary-strong); }
.per { font-style: normal; font-size: 12px; color: var(--text-dim); font-weight: 400; }
.desc { font-size: 13px; color: var(--text-dim); margin: 0; }
.specs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; padding: 12px 0; border-top: 1px dashed var(--line); border-bottom: 1px dashed var(--line); }
.spec { display: flex; justify-content: space-between; font-size: 13px; }
.spec .k { color: var(--text-dim); }
.spec .v { font-weight: 600; }
.buy { width: 100%; }
.note { font-size: 12px; margin-top: 16px; }
.empty { padding: 60px 0; text-align: center; font-size: 13px; color: var(--text-dim); }
.clickable { cursor: pointer; }
.detail-link {
  width: 100%; border: none; background: transparent; color: var(--text-dim);
  font-size: 12px; cursor: pointer; font-family: inherit; padding: 0; margin-top: -4px;
  transition: color .15s;
}
.detail-link:hover { color: var(--primary-strong); }

/* 商品详情弹窗 */
.dt { display: flex; flex-direction: column; gap: 14px; }
.dt-head { display: flex; align-items: center; gap: 14px; }
.env-badge {
  flex: none; width: 52px; height: 52px; border-radius: 14px; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, var(--primary), var(--primary-deep, #0b5c4a));
  color: #fff; font-weight: 700; font-size: 12.5px;
  box-shadow: 0 6px 16px -6px color-mix(in srgb, var(--primary) 60%, transparent);
}
.dt-title h3 { margin: 0 0 4px; font-size: 17px; font-weight: 800; }
.dt-price { margin: 0; font-size: 22px; font-weight: 800; color: var(--primary-strong); }
.dt-desc { margin: 0; font-size: 13px; line-height: 1.75; color: var(--text-2); }
.dt-specs { border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
.dt-row {
  display: flex; justify-content: space-between; align-items: center; padding: 10px 14px;
  font-size: 13px; background: var(--panel);
}
.dt-row + .dt-row { border-top: 1px solid var(--line); }
.dt-row span { color: var(--text-dim); }
.dt-row b { font-weight: 600; }
.dt-perks { display: flex; flex-wrap: wrap; gap: 8px; }
.perk {
  display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600;
  color: var(--primary-strong); background: var(--primary-soft, #e8f6f1);
  border: 1px solid color-mix(in srgb, var(--primary) 22%, transparent);
  border-radius: 999px; padding: 4px 11px;
}
.dt-tip { font-size: 12px; margin: 0; }
.dt-discount {
  display: flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600;
  color: var(--primary-strong); background: var(--primary-soft, #e8f6f1);
  border: 1px dashed color-mix(in srgb, var(--primary) 35%, transparent);
  border-radius: 10px; padding: 8px 12px;
}
.dt-coupon { display: flex; gap: 8px; }
.dt-pay {
  margin: 0; font-size: 13px; display: flex; justify-content: space-between; align-items: center;
}
.dt-pay b { font-size: 16px; color: var(--primary-strong); }
.dt-pay.err { color: var(--danger, #e5484d); font-size: 12.5px; justify-content: flex-start; }
</style>
