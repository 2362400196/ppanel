<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITable from '../components/ui/UITable.vue'
import UITag from '../components/ui/UITag.vue'
import UIModal from '../components/ui/UIModal.vue'
import UIInput from '../components/ui/UIInput.vue'
import UISelect from '../components/ui/UISelect.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'

const plans = ref([])
const nodes = ref([])
const images = ref([])  // 运行环境候选（全局白名单）
const planCols = [
  { key: 'id', label: 'ID', width: '56px' },
  { key: 'name', label: '名称', width: '110px' },
  { key: 'desc', label: '描述' },
  { key: 'spec', label: '规格', width: '190px' },
  { key: 'image', label: '运行环境', width: '150px' },
  { key: 'days', label: '有效期', width: '90px' },
  { key: 'price', label: '价格', width: '90px' },
  { key: 'node', label: '节点', width: '120px' },
  { key: 'sort', label: '排序', width: '70px' },
  { key: 'state', label: '状态', width: '100px' },
  { key: 'ops', label: '', width: '170px' }
]
const planEditOpen = ref(false)
const planEditing = ref(null)          // null=新建，否则为编辑目标
const planForm = ref({})
const savingPlan = ref(false)
const planDelOpen = ref(false)
const planDelTarget = ref(null)

const priceYuan = cents => (cents / 100).toFixed(cents % 100 ? 2 : 0)
const nodeMap = computed(() => Object.fromEntries(nodes.value.map(n => [n.id, n.name])))

async function loadNodes() {
  try {
    const { data } = await api.get('/admin/nodes')
    nodes.value = data.filter(n => n.enabled)
  } catch { /* 节点下拉失败不阻塞页面 */ }
}

async function loadImages() {
  try {
    const { data } = await api.get('/images')
    images.value = data
  } catch { /* 环境下拉失败不阻塞页面 */ }
}

function blankPlan() {
  return { name: '', desc: '', cpu: 1, mem: 512, disk: 2048, days: 30, traffic_gb: 0, price: '5.00', sort: 0, node_id: null, image: images.value[0] || 'python:3.11-slim', enabled: true }
}

async function loadPlans() {
  try {
    const { data } = await api.get('/admin/plans')
    plans.value = data
  } catch (e) {
    toastErr(errText(e))
  }
}

function openPlanCreate() {
  planEditing.value = null
  planForm.value = blankPlan()
  planEditOpen.value = true
}

function openPlanEdit(row) {
  planEditing.value = row
  planForm.value = { ...row, price: priceYuan(row.price_cents), node_id: row.node_id || null, image: row.image || images.value[0] || 'python:3.11-slim' }
  planEditOpen.value = true
}

async function savePlan() {
  const f = planForm.value
  if (!f.name.trim()) {
    toastErr('商品名称不能为空')
    return
  }
  const cents = Math.round(parseFloat(f.price || '0') * 100)
  if (!Number.isFinite(cents) || cents < 0) {
    toastErr('价格格式不正确')
    return
  }
  savingPlan.value = true
  const body = {
    name: f.name.trim(),
    desc: f.desc || '',
    cpu: Number(f.cpu),
    mem: Number(f.mem),
    disk: Number(f.disk),
    days: Number(f.days),
    traffic_gb: Number(f.traffic_gb) || 0,
    price_cents: cents,
    sort: Number(f.sort) || 0,
    node_id: f.node_id || null,
    image: f.image || '',
    enabled: !!f.enabled,
  }
  try {
    if (planEditing.value) {
      await api.put(`/admin/plans/${planEditing.value.id}`, body)
      toastOk('商品已更新')
    } else {
      await api.post('/admin/plans', body)
      toastOk('商品已创建')
    }
    planEditOpen.value = false
    loadPlans()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    savingPlan.value = false
  }
}

async function togglePlan(row) {
  try {
    const { data } = await api.post(`/admin/plans/${row.id}/toggle`)
    toastOk(data.enabled ? '「已上架」' : '「已下架」')
    loadPlans()
  } catch (e) {
    toastErr(errText(e))
  }
}

async function doDeletePlan() {
  try {
    await api.delete(`/admin/plans/${planDelTarget.value.id}`)
    toastOk('商品已删除')
    planDelOpen.value = false
    loadPlans()
  } catch (e) {
    toastErr(errText(e))
  }
}

onMounted(() => {
  loadPlans()
  loadNodes()
  loadImages()
})
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">商品管理</h1>
        <p class="page-sub">商城货架的商品配置：规格、价格、有效期与上下架</p>
      </div>
    </div>

    <div class="sec-head">
      <span class="text-dim">共 {{ plans.length }} 个商品（下架商品不在商城展示）</span>
      <UIButton @click="openPlanCreate">新建商品</UIButton>
    </div>

    <UITable :columns="planCols" :rows="plans">
      <template #col-spec="{ row }">{{ row.cpu }} 核 / {{ row.mem }}MB / {{ row.disk / 1024 }}GB / {{ row.traffic_gb ? row.traffic_gb + 'GB流量' : '流量不限' }}</template>
      <template #col-days="{ row }">{{ row.days }} 天</template>
      <template #col-price="{ row }">¥{{ priceYuan(row.price_cents) }}</template>
      <template #col-image="{ row }">
        <span class="mono">{{ row.image || 'python:3.11-slim' }}</span>
      </template>
      <template #col-node="{ row }">
        <span :class="{ 'text-dim': !row.node_id }">{{ row.node_id ? (nodeMap[row.node_id] || `节点#${row.node_id}`) : '自动分配' }}</span>
      </template>
      <template #col-state="{ row }">
        <UITag :tone="row.enabled ? 'ok' : 'dim'">{{ row.enabled ? '上架中' : '已下架' }}</UITag>
      </template>
      <template #col-ops="{ row }">
        <UIButton type="text" @click="openPlanEdit(row)">编辑</UIButton>
        <UIButton type="text" @click="togglePlan(row)">{{ row.enabled ? '下架' : '上架' }}</UIButton>
        <UIButton type="text" class="danger" @click="planDelTarget = row; planDelOpen = true">删除</UIButton>
      </template>
    </UITable>

    <!-- 新建/编辑商品 -->
    <UIModal v-model:open="planEditOpen" :title="planEditing ? '编辑商品' : '新建商品'" width="440px">
      <div class="form">
        <div class="row2">
          <label class="field">
            <span>名称</span>
            <UIInput v-model="planForm.name" placeholder="如：轻量型" />
          </label>
          <label class="field">
            <span>价格（元）</span>
            <UIInput v-model="planForm.price" placeholder="5.00" />
          </label>
        </div>
        <label class="field">
          <span>描述</span>
          <UIInput v-model="planForm.desc" placeholder="适合场景一句话" />
        </label>
        <div class="row2 row3">
          <label class="field">
            <span>CPU（核）</span>
            <UIInput v-model="planForm.cpu" type="number" />
          </label>
          <label class="field">
            <span>内存（MB）</span>
            <UIInput v-model="planForm.mem" type="number" />
          </label>
          <label class="field">
            <span>磁盘（MB）</span>
            <UIInput v-model="planForm.disk" type="number" />
          </label>
        </div>
        <div class="row2">
          <label class="field">
            <span>运行环境（买家开通即用）</span>
            <UISelect
              v-model="planForm.image"
              :options="images.map(i => ({ value: i, label: i }))"
            />
          </label>
          <label class="field">
            <span>绑定节点</span>
            <UISelect
              v-model="planForm.node_id"
              :options="[{ value: null, label: '自动分配' }, ...nodes.map(n => ({ value: n.id, label: n.name }))]"
            />
          </label>
        </div>
        <div class="row2 row4">
          <label class="field">
            <span>有效期（天）</span>
            <UIInput v-model="planForm.days" type="number" />
          </label>
          <label class="field">
            <span>月流量（GB，0=不限）</span>
            <UIInput v-model="planForm.traffic_gb" type="number" />
          </label>
          <label class="field">
            <span>排序（小在前）</span>
            <UIInput v-model="planForm.sort" type="number" />
          </label>
          <label class="field">
            <span>上架</span>
            <UISelect v-model="planForm.enabled" :options="[{ value: true, label: '上架' }, { value: false, label: '下架' }]" />
          </label>
        </div>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="planEditOpen = false">取消</UIButton>
        <UIButton :loading="savingPlan" @click="savePlan">{{ planEditing ? '保存' : '创建' }}</UIButton>
      </template>
    </UIModal>

    <ConfirmDialog
      v-model:open="planDelOpen"
      title="删除商品"
      :message="`确定删除商品「${planDelTarget?.name}」吗？已开通的实例不受影响。`"
      confirm-text="删除"
      danger
      @confirm="doDeletePlan"
    />
  </div>
</template>

<style scoped>
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.row2.row3 { grid-template-columns: 1fr 1fr 1fr; }
.row2.row4 { grid-template-columns: 1fr 1fr 1fr 1fr; }
</style>
