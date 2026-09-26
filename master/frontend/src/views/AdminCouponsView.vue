<script setup>
// 管理员优惠券：创建（面额/门槛/数量/有效期）+ 列表 + 停用
import { onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UITag from '../components/ui/UITag.vue'
import UITable from '../components/ui/UITable.vue'
import UIModal from '../components/ui/UIModal.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import { fmtTime } from '../utils/format'

const cols = [
  { key: 'code', label: '券码', width: '150px' },
  { key: 'amount', label: '面额', width: '90px' },
  { key: 'min', label: '使用门槛', width: '100px' },
  { key: 'used', label: '核销 / 总量', width: '110px' },
  { key: 'expire_at', label: '有效期至', width: '170px' },
  { key: 'note', label: '备注' },
  { key: 'status', label: '状态', width: '90px' },
  { key: 'ops', label: '', width: '90px' },
]

const rows = ref([])
const loading = ref(true)

function toRow(c) {
  const expired = c.expire_at && new Date(c.expire_at) < new Date()
  return {
    ...c, amount: `¥${(c.amount_cents / 100).toFixed(c.amount_cents % 100 ? 2 : 0)}`,
    min: c.min_spend_cents ? `满 ¥${c.min_spend_cents / 100}` : '无门槛',
    used: `${c.used} / ${c.total}`,
    status: !c.enabled ? '已停用' : expired ? '已过期' : c.left <= 0 ? '已抢完' : '生效中',
    _tone: !c.enabled || expired ? 'info' : c.left <= 0 ? 'warn' : 'ok',
    _canDisable: c.enabled && !expired && c.left > 0,
  }
}

async function load() {
  try {
    const { data } = await api.get('/admin/rewards/coupons')
    rows.value = data.map(toRow)
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

// 创建
const createOpen = ref(false)
const form = ref({ amount_yuan: 5, min_spend_yuan: 0, total: 10, expire_days: 30, note: '' })
const creating = ref(false)
const created = ref(null)   // 刚创建的券（展示码）

async function doCreate() {
  if (creating.value) return
  creating.value = true
  try {
    const { data } = await api.post('/admin/rewards/coupons', form.value)
    created.value = data
    toastOk(`优惠券已创建：${data.code}`)
    await load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    creating.value = false
  }
}

// 停用
const disableOpen = ref(false)
const disableTarget = ref(null)
function askDisable(row) { disableTarget.value = row; disableOpen.value = true }

async function doDisable() {
  try {
    await api.post(`/admin/rewards/coupons/${disableTarget.value.id}/disable`)
    toastOk('优惠券已停用')
    disableOpen.value = false
    load()
  } catch (e) {
    toastErr(errText(e))
  }
}

function copyCode(code) {
  navigator.clipboard?.writeText(code)
  toastOk('券码已复制')
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">优惠券</h1>
        <p class="page-sub">创建活动券码，用户在商城购买时输入抵扣（券码一次性核销）</p>
      </div>
      <UIButton @click="createOpen = true; created = null">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14" /></svg>
        创建优惠券
      </UIButton>
    </div>

    <UITable :columns="cols" :rows="rows" :loading="loading">
      <template #col-code="{ row }">
        <button class="code mono" title="点击复制券码" @click="copyCode(row.code)">{{ row.code }}</button>
      </template>
      <template #col-status="{ row }">
        <UITag :tone="row._tone">{{ row.status }}</UITag>
      </template>
      <template #col-note="{ row }"><span class="dim">{{ row.note || '—' }}</span></template>
      <template #col-ops="{ row }">
        <UIButton v-if="row._canDisable" type="text" class="danger" @click="askDisable(row)">停用</UIButton>
      </template>
    </UITable>

    <!-- 创建 -->
    <UIModal v-model:open="createOpen" :title="created ? '优惠券已创建' : '创建优惠券'" width="420px">
      <div v-if="!created" class="form">
        <label class="field"><span>抵扣面额（元）</span><UIInput v-model.number="form.amount_yuan" type="number" :min="0.01" /></label>
        <label class="field"><span>使用门槛（商品原价满多少元可用，0 为无门槛）</span><UIInput v-model.number="form.min_spend_yuan" type="number" :min="0" /></label>
        <div class="half">
          <label class="field"><span>发放数量</span><UIInput v-model.number="form.total" type="number" :min="1" /></label>
          <label class="field"><span>有效期（天）</span><UIInput v-model.number="form.expire_days" type="number" :min="1" /></label>
        </div>
        <label class="field"><span>备注（活动名）</span><UIInput v-model="form.note" placeholder="如：周年庆活动" /></label>
      </div>
      <div v-else class="created">
        <p class="cd-label text-dim">券码（点击复制，发给用户）</p>
        <button class="cd-code mono" @click="copyCode(created.code)">{{ created.code }}</button>
        <p class="cd-desc text-dim">面额 ¥{{ (created.amount_cents / 100).toFixed(2) }} · {{ created.min_spend_cents ? `满 ¥${created.min_spend_cents / 100} 可用` : '无门槛' }} · 共 {{ created.total }} 张</p>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="createOpen = false">{{ created ? '完成' : '取消' }}</UIButton>
        <UIButton v-if="!created" :loading="creating" @click="doCreate">创建</UIButton>
      </template>
    </UIModal>

    <ConfirmDialog v-model:open="disableOpen" title="停用优惠券"
                   :message="`确定停用券码「${disableTarget?.code}」吗？停用后用户无法再使用（已核销的不受影响）。`"
                   confirm-text="停用" @confirm="doDisable" />
  </div>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.code { border: none; background: transparent; cursor: pointer; color: var(--primary-strong); font-weight: 700; font-size: 13px; letter-spacing: 1px; font-family: inherit; padding: 0; }
.code:hover { text-decoration: underline; }
.dim { color: var(--text-dim); font-size: 12.5px; }
.danger { color: var(--danger, #e5484d); }
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
.half { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.created { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 8px 0; }
.cd-label { margin: 0; font-size: 12px; }
.cd-code {
  font-size: 26px; font-weight: 800; letter-spacing: 3px; color: var(--primary-strong);
  background: var(--primary-soft, #e8f6f1); border: 1px dashed color-mix(in srgb, var(--primary) 40%, transparent);
  border-radius: 12px; padding: 12px 28px; cursor: pointer; font-family: inherit;
}
.cd-desc { margin: 0; font-size: 12.5px; }
</style>
