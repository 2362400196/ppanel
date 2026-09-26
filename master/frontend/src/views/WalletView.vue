<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UITag from '../components/ui/UITag.vue'
import UITable from '../components/ui/UITable.vue'
import UIModal from '../components/ui/UIModal.vue'
import { fmtTime } from '../utils/format'

// ---------- 余额与流水 ----------
const balanceCents = ref(0)
const payEnabled = ref(false)
const records = ref([])
const loading = ref(true)
const recCols = [
  { key: 'kind', label: '类型', width: '90px' },
  { key: 'title', label: '说明' },
  { key: 'amount', label: '金额', width: '110px' },
  { key: 'status', label: '状态', width: '96px' },
  { key: 'created_at', label: '时间', width: '170px' },
]

const statusMap = {
  pending: { text: '待支付', tone: 'warn' },
  paid: { text: '完成', tone: 'ok' },
  failed: { text: '失败', tone: 'danger' },
}

async function loadWallet() {
  try {
    const { data } = await api.get('/wallet')
    balanceCents.value = data.balance_cents
    payEnabled.value = data.pay_enabled
    const rs = await api.get('/wallet/records')
    records.value = rs.data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}

onMounted(loadWallet)

const balanceText = computed(() =>
  `¥${(balanceCents.value / 100).toFixed(balanceCents.value % 100 ? 2 : 0)}`)

// ---------- 微信充值（Native 扫码 + 轮询查单） ----------
const QUICK = [1000, 3000, 5000, 10000]  // ¥10/30/50/100
const rcOpen = ref(false)
const amountYuan = ref('10')
const rcNo = ref('')
const rcQr = ref('')
const rcState = ref('pending')  // pending | success | failed
const creating = ref(false)
let rcTimer = null

function openRecharge() {
  if (!payEnabled.value) {
    toastErr('微信充值未开启，请联系管理员配置')
    return
  }
  rcState.value = 'pending'
  rcOpen.value = true
  createOrder()
}

async function createOrder() {
  clearInterval(rcTimer)
  const cents = Math.round(parseFloat(amountYuan.value || '0') * 100)
  if (!(cents > 0)) { toastErr('请输入正确的充值金额'); return }
  creating.value = true
  try {
    const { data } = await api.post('/wallet/recharge', { amount_cents: cents })
    rcNo.value = data.out_trade_no
    const qr = await api.get(`/wallet/recharge/${data.out_trade_no}/qrcode`, { responseType: 'blob' })
    if (rcQr.value) URL.revokeObjectURL(rcQr.value)
    rcQr.value = URL.createObjectURL(qr.data)
    rcState.value = 'pending'
    pollState()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    creating.value = false
  }
}

function pollState() {
  clearInterval(rcTimer)
  rcTimer = setInterval(async () => {
    if (!rcNo.value) return
    try {
      const { data } = await api.get(`/wallet/recharge/${rcNo.value}`)
      if (data.trade_state === 'SUCCESS') {
        rcState.value = 'success'
        balanceCents.value = data.balance_cents
        clearInterval(rcTimer)
        toastOk('充值到账')
        loadWallet()
      } else if (data.trade_state === 'PAYERROR' || data.trade_state === 'CLOSED') {
        rcState.value = 'failed'
        clearInterval(rcTimer)
      }
    } catch { /* 单次轮询失败忽略 */ }
  }, 2500)
}

onUnmounted(() => clearInterval(rcTimer))

function closeRc() {
  clearInterval(rcTimer)
  if (rcQr.value) { URL.revokeObjectURL(rcQr.value); rcQr.value = '' }
  rcNo.value = ''
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">钱包</h1>
        <p class="page-sub">余额充值与消费流水，商城购买从余额扣款</p>
      </div>
    </div>

    <!-- 余额卡 -->
    <div class="wallet-card">
      <div class="wallet-left">
        <p class="w-label">当前余额</p>
        <p class="w-balance mono">{{ balanceText }}</p>
      </div>
      <UIButton class="w-btn" :disabled="!payEnabled" @click="openRecharge">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14" /></svg>
        微信充值
      </UIButton>
    </div>

    <!-- 流水 -->
    <h2 class="sec-title">收支流水</h2>
    <UITable :columns="recCols" :rows="records" :loading="loading">
      <template #col-kind="{ row }">
        <UITag :tone="row.kind === 'recharge' ? 'ok' : row.kind === 'admin' ? 'info' : 'primary'">
          {{ row.kind === 'recharge' ? '充值' : row.kind === 'admin' ? '调整' : '消费' }}
        </UITag>
      </template>
      <template #col-title="{ row }">{{ row.plan_name || '—' }}</template>
      <template #col-amount="{ row }">
        <span :class="row.kind === 'recharge' || row.kind === 'admin' ? 'in' : 'out'">
          {{ row.kind === 'recharge' || row.kind === 'admin' ? '+' : '−' }}¥{{ (row.amount_cents / 100).toFixed(2) }}
        </span>
      </template>
      <template #col-status="{ row }">
        <UITag :tone="statusMap[row.status]?.tone || 'info'">{{ statusMap[row.status]?.text || row.status }}</UITag>
      </template>
      <template #col-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
    </UITable>

    <!-- 微信充值弹窗 -->
    <UIModal v-model:open="rcOpen" title="微信扫码充值" width="420px" @close="closeRc">
      <div class="pay-body">
        <template v-if="rcState !== 'success'">
          <div class="quick-row">
            <button v-for="c in QUICK" :key="c" class="quick-btn" :class="{ active: amountYuan === String(c / 100) }"
                    @click="amountYuan = String(c / 100)">¥{{ c / 100 }}</button>
          </div>
          <div class="amt-row">
            <UIInput v-model="amountYuan" style="width:150px" placeholder="自定义金额（元）" />
            <UIButton type="ghost" :loading="creating" @click="createOrder">重新生成二维码</UIButton>
          </div>
          <div class="qr-wrap" :class="{ dim: rcState === 'failed' }">
            <img v-if="rcQr" :src="rcQr" class="qr" alt="微信支付二维码">
            <div v-else class="qr-loading">生成中…</div>
            <div v-if="rcState === 'failed'" class="qr-mask">订单已关闭，请重新生成</div>
          </div>
          <p class="pay-tip">请使用微信扫码支付，支付成功后余额自动到账</p>
        </template>
        <template v-else>
          <div class="pay-ok">
            <div class="ok-ring">
              <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 7" /></svg>
            </div>
            <p class="ok-title">充值成功</p>
            <p class="ok-sub">余额已更新为 {{ balanceText }}</p>
          </div>
        </template>
      </div>
    </UIModal>
  </div>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }

.wallet-card {
  display: flex; align-items: center; justify-content: space-between;
  padding: 24px 26px; border-radius: var(--radius);
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-strong, #0f7a63) 100%);
  color: #fff; box-shadow: var(--shadow-lg); margin-bottom: 22px;
}
.w-label { font-size: 13px; opacity: .85; margin: 0 0 6px; }
.w-balance { font-size: 34px; font-weight: 800; margin: 0; font-variant-numeric: tabular-nums; }
.w-btn { background: rgba(255,255,255,.92); color: var(--primary-deep, #0b5c4a); border: none; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
.w-btn:hover { background: #fff; }

.sec-title { font-size: 14px; font-weight: 700; margin: 0 0 12px; color: var(--text-2); }
.in { color: var(--ok, #30a46c); font-weight: 600; font-variant-numeric: tabular-nums; }
.out { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }

/* 充值弹窗 */
.pay-body { text-align: center; padding: 4px 0 2px; }
.quick-row { display: flex; justify-content: center; gap: 8px; margin-bottom: 12px; }
.quick-btn {
  border: 1px solid var(--line); background: var(--panel); border-radius: 10px;
  padding: 7px 14px; font-size: 13px; cursor: pointer; color: var(--text-2); font-family: inherit;
  transition: all .15s;
}
.quick-btn:hover { border-color: var(--primary); color: var(--primary-strong); }
.quick-btn.active { border-color: var(--primary); background: var(--primary-soft); color: var(--primary-strong); font-weight: 700; }
.amt-row { display: flex; justify-content: center; align-items: center; gap: 10px; margin-bottom: 14px; }
.qr-wrap { position: relative; width: 210px; height: 210px; margin: 0 auto; padding: 10px; border: 1px solid var(--line); border-radius: 14px; background: #fff; }
.qr { width: 100%; height: 100%; display: block; image-rendering: pixelated; }
.qr-loading { display: flex; align-items: center; justify-content: center; height: 100%; font-size: 12.5px; color: var(--text-dim); }
.qr-mask { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; background: rgba(255,255,255,.88); backdrop-filter: blur(3px); border-radius: 14px; font-size: 13.5px; font-weight: 600; color: var(--danger, #e5484d); padding: 0 14px; text-align: center; }
.pay-tip { font-size: 13px; color: var(--text-2); margin: 14px 0 4px; }
.pay-ok { padding: 18px 0 26px; }
.ok-ring {
  width: 74px; height: 74px; margin: 0 auto 14px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  color: var(--ok, #30a46c); background: color-mix(in srgb, var(--ok, #30a46c) 12%, transparent);
  border: 2px solid var(--ok, #30a46c); animation: ok-pop .5s cubic-bezier(.2, 1.4, .4, 1);
}
.ok-title { font-size: 18px; font-weight: 700; margin: 0 0 6px; }
.ok-sub { font-size: 13px; color: var(--text-dim); margin: 0; }
@keyframes ok-pop { from { transform: scale(.4); opacity: 0; } to { transform: scale(1); opacity: 1; } }
</style>
