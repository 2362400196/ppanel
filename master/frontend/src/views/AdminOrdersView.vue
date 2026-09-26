<script setup>
import { onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UISelect from '../components/ui/UISelect.vue'
import UITag from '../components/ui/UITag.vue'
import UITable from '../components/ui/UITable.vue'
import UIModal from '../components/ui/UIModal.vue'
import { fmtTime } from '../utils/format'

// ---------- 支付配置 ----------
const cfgOpen = ref(false)
const cfg = ref({ enabled: '0', appid: '', mchid: '', serial: '', notify_url: '', has_key: false })
const pemText = ref('')
const savingCfg = ref(false)
const enabledOpts = [
  { label: '启用（商城走微信支付）', value: '1' },
  { label: '停用（商城免费直接开通）', value: '0' },
]

async function loadCfg() {
  try {
    const { data } = await api.get('/admin/pay/config')
    cfg.value = { ...data, enabled: data.enabled ? '1' : '0' }
  } catch (e) {
    toastErr(errText(e))
  }
}

async function openCfg() {
  await loadCfg()
  cfgOpen.value = true
}

async function saveCfg() {
  if (savingCfg.value) return
  savingCfg.value = true
  try {
    const body = {
      enabled: cfg.value.enabled === '1',
      appid: cfg.value.appid, mchid: cfg.value.mchid,
      serial: cfg.value.serial, notify_url: cfg.value.notify_url,
      private_key_pem: pemText.value,   // 留空 = 保留原私钥
    }
    const { data } = await api.put('/admin/pay/config', body)
    toastOk(data.detail || '已保存')
    if (data.enabled !== undefined) cfg.value.has_key = cfg.value.has_key || !!pemText.value.trim()
    pemText.value = ''
    cfgOpen.value = false
    loadOrders()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    savingCfg.value = false
  }
}

// ---------- 订单列表 ----------
const orders = ref([])
const loading = ref(true)
const orderCols = [
  { key: 'out_trade_no', label: '单号' },
  { key: 'username', label: '用户', width: '100px' },
  { key: 'kind', label: '类型', width: '76px' },
  { key: 'plan_name', label: '说明', width: '130px' },
  { key: 'amount', label: '金额', width: '90px' },
  { key: 'balance', label: '用户余额', width: '96px' },
  { key: 'status', label: '状态', width: '92px' },
  { key: 'created_at', label: '时间', width: '170px' },
  { key: 'ops', label: '', width: '70px' },
]

const statusMap = {
  pending: { text: '待支付', tone: 'warn' },
  paid: { text: '已入账', tone: 'ok' },
  refunded: { text: '已退款', tone: 'info' },
  failed: { text: '失败', tone: 'danger' },
}
const kindMap = { recharge: '充值', shop: '消费', admin: '调整', refund: '退款' }

async function loadOrders() {
  try {
    const { data } = await api.get('/admin/pay/orders')
    orders.value = data
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}

onMounted(() => { loadOrders(); loadCfg() })

// ---------- 退款 ----------
const refundOpen = ref(false)
const refundTarget = ref(null)
const refundAmount = ref('')      // 元字符串
const refundDoing = ref(false)

function askRefund(row) {
  refundTarget.value = row
  refundAmount.value = (row.amount_cents / 100).toFixed(2)
  refundOpen.value = true
}

async function doRefund() {
  if (refundDoing.value) return
  const cents = Math.round(parseFloat(refundAmount.value) * 100)
  if (!(cents > 0)) { toastErr('退款金额无效'); return }
  refundDoing.value = true
  try {
    const { data } = await api.post('/admin/pay/refund',
      { out_trade_no: refundTarget.value.out_trade_no, amount_cents: cents })
    toastOk(data.detail || '退款成功')
    refundOpen.value = false
    loadOrders()
    loadCfg()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    refundDoing.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">订单支付</h1>
        <p class="page-sub">微信支付订单与商户配置，支付成功自动开通实例</p>
      </div>
      <div class="head-right">
        <UITag :tone="cfg.enabled === '1' ? 'ok' : 'info'">
          {{ cfg.enabled === '1' ? '微信支付已启用' : '微信支付未启用' }}
        </UITag>
        <UIButton type="ghost" @click="openCfg">支付配置</UIButton>
        <UIButton @click="loadOrders">刷新</UIButton>
      </div>
    </div>

    <UITable :columns="orderCols" :rows="orders" :loading="loading">
      <template #col-kind="{ row }">
        <UITag :tone="row.kind === 'recharge' ? 'ok' : row.kind === 'refund' ? 'danger' : row.kind === 'admin' ? 'info' : 'primary'">
          {{ kindMap[row.kind] || row.kind }}
        </UITag>
      </template>
      <template #col-amount="{ row }">¥{{ (row.amount_cents / 100).toFixed(2) }}</template>
      <template #col-balance="{ row }">¥{{ ((row.balance_cents || 0) / 100).toFixed(2) }}</template>
      <template #col-status="{ row }">
        <UITag :tone="statusMap[row.status]?.tone || 'info'">{{ statusMap[row.status]?.text || row.status }}</UITag>
      </template>
      <template #col-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
      <template #col-ops="{ row }">
        <UIButton v-if="row.kind === 'recharge' && row.status === 'paid'"
                  type="text" class="danger" @click="askRefund(row)">退款</UIButton>
      </template>
    </UITable>

    <!-- 退款确认弹窗 -->
    <UIModal v-model:open="refundOpen" title="订单退款" width="460px">
      <div class="refund-body">
        <p class="refund-info">
          单号 <b>{{ refundTarget?.out_trade_no }}</b> · 用户 <b>{{ refundTarget?.username }}</b><br />
          充值金额 ¥{{ ((refundTarget?.amount_cents || 0) / 100).toFixed(2) }}
        </p>
        <label class="cfg-label">退款金额（元，支持部分退款）</label>
        <UIInput v-model="refundAmount" type="number" />
        <p class="refund-tip">退款将原路退回用户微信零钱，同时从用户钱包余额等额扣回。此操作经微信商户平台执行，不可撤销。</p>
        <div class="refund-foot">
          <UIButton type="ghost" @click="refundOpen = false">取消</UIButton>
          <UIButton class="danger-btn" :loading="refundDoing" @click="doRefund">确认退款</UIButton>
        </div>
      </div>
    </UIModal>

    <!-- 微信支付配置弹窗 -->
    <UIModal v-model:open="cfgOpen" title="微信支付配置" width="560px">
      <div class="cfg-body">
        <p class="cfg-tip">参数来自微信商户平台（账户中心 → API 安全）。私钥只存服务器，不会再次显示。</p>
        <label class="cfg-label">状态</label>
        <UISelect v-model="cfg.enabled" :options="enabledOpts" />
        <div class="cfg-grid">
          <div>
            <label class="cfg-label">AppID（公众号/小程序）</label>
            <UIInput v-model="cfg.appid" placeholder="wx…" />
          </div>
          <div>
            <label class="cfg-label">商户号 mchid</label>
            <UIInput v-model="cfg.mchid" placeholder="16…" />
          </div>
          <div>
            <label class="cfg-label">证书序列号</label>
            <UIInput v-model="cfg.serial" placeholder="商户 API 证书序列号" />
          </div>
          <div>
            <label class="cfg-label">回调地址 notify_url</label>
            <UIInput v-model="cfg.notify_url" placeholder="https://你的域名/api/pay/notify" />
          </div>
        </div>
        <label class="cfg-label">
          商户私钥（apiclient_key.pem）
          <span class="cfg-sub">{{ cfg.has_key ? '已保存 ✓，留空则不修改' : '未配置' }}</span>
        </label>
        <textarea v-model="pemText" class="pem-area" rows="6"
                  placeholder="-----BEGIN PRIVATE KEY-----&#10;…&#10;-----END PRIVATE KEY-----" />
        <div class="cfg-foot">
          <UIButton :loading="savingCfg" @click="saveCfg">保存配置</UIButton>
        </div>
      </div>
    </UIModal>
  </div>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.head-right { display: flex; align-items: center; gap: 10px; }

.cfg-body { display: flex; flex-direction: column; gap: 8px; }
.cfg-tip { font-size: 12px; color: var(--text-dim); margin: 0 0 4px; line-height: 1.6; }
.cfg-label { font-size: 12.5px; font-weight: 600; color: var(--text-2); margin-top: 6px; }
.cfg-sub { font-weight: 400; font-size: 12px; color: var(--ok, #30a46c); margin-left: 8px; }
.cfg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 14px; }
.pem-area {
  width: 100%; box-sizing: border-box; font-family: ui-monospace, Consolas, monospace; font-size: 12px;
  border: 1px solid var(--line); border-radius: var(--radius); background: var(--bg);
  padding: 10px 12px; color: var(--text); resize: vertical; outline: none; line-height: 1.6;
}
.pem-area:focus { border-color: var(--primary); }
.cfg-foot { display: flex; justify-content: flex-end; margin-top: 10px; }

.refund-body { display: flex; flex-direction: column; gap: 8px; }
.refund-info { margin: 0; font-size: 13px; line-height: 1.8; color: var(--text-2); }
.refund-info b { color: var(--text); }
.refund-tip { margin: 2px 0 4px; font-size: 12px; line-height: 1.7; color: var(--text-dim); }
.refund-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 6px; }
.danger-btn { --btn-bg: #e5484d; }
.danger { color: var(--danger, #e5484d); }
</style>
