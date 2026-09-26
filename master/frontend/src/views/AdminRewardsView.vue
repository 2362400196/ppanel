<script setup>
import { onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'

const loading = ref(true)
const saving = ref(false)
const checkinPoints = ref(10)
const earnPerYuan = ref(10)
const pointsPerDay = ref(100)
// 等级行：{ exp_yuan: 累计消费(元), pct: 折扣% }，行序即等级序号
const levels = ref([])

async function load() {
  try {
    const { data } = await api.get('/admin/rewards/config')
    checkinPoints.value = data.checkin_points
    earnPerYuan.value = data.earn_per_yuan
    pointsPerDay.value = data.points_per_day
    levels.value = (data.levels || []).map(l => ({
      exp_yuan: l.exp / 100,
      pct: l.pct,
    }))
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}

function addLevel() {
  const last = levels.value[levels.value.length - 1]
  levels.value.push({
    exp_yuan: last ? Number(last.exp_yuan) + 100 : 0,
    pct: last ? Math.max(50, Number(last.pct) - 3) : 100,
  })
}

function removeLevel(i) {
  if (levels.value.length <= 1) return
  levels.value.splice(i, 1)
}

async function save() {
  // 前端先校验一遍，给用户直观报错
  for (const [i, l] of levels.value.entries()) {
    if (l.exp_yuan === '' || l.pct === '') return toastErr(`第 ${i + 1} 级有未填写的字段`)
  }
  for (const [i, l] of levels.value.entries()) {
    if (i > 0 && Number(l.exp_yuan) <= Number(levels.value[i - 1].exp_yuan)) {
      return toastErr(`第 ${i + 1} 级的消费门槛必须大于上一级`)
    }
  }
  saving.value = true
  try {
    const { data } = await api.put('/admin/rewards/config', {
      checkin_points: Number(checkinPoints.value) || 0,
      earn_per_yuan: Number(earnPerYuan.value) || 0,
      points_per_day: Number(pointsPerDay.value) || 1,
      levels: levels.value.map((l, i) => ({
        lv: i + 1,
        exp: Math.round(Number(l.exp_yuan) * 100),
        pct: Math.round(Number(l.pct)),
      })),
    })
    toastOk(data.detail || '已保存')
    load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">福利设置</h1>
        <p class="page-sub">签到积分、消费返积分、积分兑换比例与会员等级折扣，保存后立即对全站生效</p>
      </div>
      <UIButton type="primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存设置' }}</UIButton>
    </div>

    <div v-if="loading" class="loading">加载中…</div>

    <template v-else>
      <!-- 积分规则 -->
      <div class="card block">
        <div class="block-title">积分规则</div>
        <div class="rule-grid">
          <label class="field">
            <span class="f-label">每日签到积分</span>
            <UIInput v-model="checkinPoints" type="number" />
            <span class="f-hint">用户每天签到固定获得</span>
          </label>
          <label class="field">
            <span class="f-label">每实付 1 元返积分</span>
            <UIInput v-model="earnPerYuan" type="number" />
            <span class="f-hint">商城消费按实付金额返积分</span>
          </label>
          <label class="field">
            <span class="f-label">兑换 1 天所需积分</span>
            <UIInput v-model="pointsPerDay" type="number" />
            <span class="f-hint">用户用积分为实例续期</span>
          </label>
        </div>
      </div>

      <!-- 等级体系 -->
      <div class="card block">
        <div class="block-title">
          会员等级
          <button class="add-btn" @click="addLevel">+ 添加等级</button>
        </div>
        <div class="lv-head">
          <span class="c-lv">等级</span>
          <span class="c-exp">累计实付满（元）</span>
          <span class="c-pct">购买折扣（%）</span>
          <span class="c-op"></span>
        </div>
        <div v-for="(l, i) in levels" :key="i" class="lv-row">
          <span class="c-lv">
            <span class="lv-badge">LV{{ i + 1 }}</span>
          </span>
          <span class="c-exp"><UIInput v-model="l.exp_yuan" type="number" /></span>
          <span class="c-pct"><UIInput v-model="l.pct" type="number" /></span>
          <span class="c-op">
            <UIButton type="text" class="danger" :disabled="levels.length <= 1" @click="removeLevel(i)">删除</UIButton>
          </span>
        </div>
        <p class="block-hint">折扣 100 = 无折扣；数值越小折扣越大（如 88 = 88 折）。累计实付是用户历史消费总额，达到门槛自动升级。</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page-head {
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 22px;
}
.loading { color: var(--text-dim); font-size: 13px; padding: 40px 0; text-align: center; }

.block { padding: 18px; margin-bottom: 16px; }
.block-title {
  font-size: 14px; font-weight: 700; margin-bottom: 14px;
  display: flex; align-items: center; justify-content: space-between;
}
.add-btn {
  border: 1px dashed var(--primary); color: var(--primary-strong);
  background: transparent; border-radius: 8px; padding: 4px 10px;
  font-size: 12px; cursor: pointer; transition: background .15s ease;
}
.add-btn:hover { background: var(--primary-soft); }

.rule-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px;
}
.field { display: flex; flex-direction: column; gap: 6px; }
.f-label { font-size: 13px; font-weight: 600; }
.f-hint { font-size: 12px; color: var(--text-dim); }

.lv-head, .lv-row {
  display: grid; grid-template-columns: 90px 1fr 1fr 70px;
  gap: 12px; align-items: center;
}
.lv-head { font-size: 12px; color: var(--text-dim); padding: 0 0 8px; }
.lv-row { padding: 6px 0; }
.lv-badge {
  display: inline-block; padding: 3px 10px; border-radius: 999px;
  background: var(--primary-soft); color: var(--primary-strong);
  font-size: 12px; font-weight: 700;
}
.block-hint { margin: 12px 0 0; font-size: 12px; color: var(--text-dim); }
</style>
