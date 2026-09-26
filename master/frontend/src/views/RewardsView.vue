<script setup>
// 福利中心：每日签到、等级与折扣、积分兑换实例天数
import { onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'
import UISelect from '../components/ui/UISelect.vue'
import { fmtTime } from '../utils/format'

const ov = ref(null)
const signing = ref(false)
const levels = [
  { lv: 1, exp: 0, pct: 100 },
  { lv: 2, exp: 1000, pct: 98 },
  { lv: 3, exp: 5000, pct: 95 },
  { lv: 4, exp: 20000, pct: 92 },
  { lv: 5, exp: 50000, pct: 88 },
]

async function load() {
  try {
    const { data } = await api.get('/rewards/overview')
    ov.value = data
  } catch (e) {
    toastErr(errText(e))
  }
}
onMounted(load)

async function doCheckin() {
  if (signing.value) return
  signing.value = true
  try {
    const { data } = await api.post('/rewards/checkin')
    toastOk(data.detail)
    await load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    signing.value = false
  }
}

// 积分兑换
const insts = ref([])
const instOpts = ref([])
const days = ref(1)
const redeeming = ref(false)

async function loadInsts() {
  try {
    const { data } = await api.get('/instances')
    insts.value = data.filter(i => i.status !== 'deleted')
    instOpts.value = insts.value.map(i => ({ label: i.name, value: i.id }))
  } catch { /* 列表失败不阻塞签到 */ }
}
onMounted(loadInsts)

const instId = ref('')
const canRedeem = () => instId.value && days.value >= 1

async function doRedeem() {
  if (!canRedeem() || redeeming.value) return
  redeeming.value = true
  try {
    const { data } = await api.post('/rewards/redeem',
      { instance_uuid: instId.value, days: days.value })
    toastOk(data.detail)
    await load()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    redeeming.value = false
  }
}

const yuan = c => `¥${(c / 100).toFixed(0)}`
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">福利中心</h1>
        <p class="page-sub">每日签到攒积分，兑换实例时长；消费升级享折扣</p>
      </div>
    </div>

    <div v-if="ov" class="grid">
      <!-- 签到卡 -->
      <div class="card checkin-card">
        <div class="c-top">
          <h3>每日签到</h3>
          <span class="pts mono">{{ ov.points }} 积分</span>
        </div>
        <p class="c-desc text-dim">连续打卡，积分可按 <b class="hl">100 积分 = 1 天</b> 兑换实例时长；商城消费每 1 元另返 <b class="hl">10 积分</b>。</p>
        <div class="week">
          <div v-for="d in 7" :key="d" class="wdot" :class="{ on: ov.recent_days.length >= d }" />
        </div>
        <UIButton class="ck-btn" :disabled="ov.checked_today" :loading="signing" @click="doCheckin">
          {{ ov.checked_today ? '今日已签到 ✓' : `立即签到 +${ov.checkin_points} 积分` }}
        </UIButton>
        <p class="c-sub text-dim">已累计签到 {{ ov.checkin_days }} 天</p>
      </div>

      <!-- 等级卡 -->
      <div class="card level-card">
        <div class="c-top">
          <h3>会员等级</h3>
          <span class="lv-tag">LV{{ ov.level }}</span>
        </div>
        <div class="lv-bar">
          <div class="lv-fill" :style="{ width: Math.min(100, ov.level_exp / (ov.next_level_exp || 1) * 100) + '%' }" />
        </div>
        <p class="c-desc text-dim">
          累计消费 ¥{{ (ov.level_exp / 100).toFixed(2) }}
          <template v-if="ov.next_level_exp">，再消费 ¥{{ ((ov.next_level_exp - ov.level_exp) / 100).toFixed(2) }} 升 LV{{ ov.level + 1 }}</template>
          <template v-else>，已是最高等级</template>
        </p>
        <div class="lv-list">
          <div v-for="l in levels" :key="l.lv" class="lv-row" :class="{ now: l.lv === ov.level }">
            <span class="lv-l">LV{{ l.lv }}</span>
            <span class="lv-e">累计 ¥{{ yuan(l.exp) }}</span>
            <span class="lv-d">{{ l.pct < 100 ? `${l.pct} 折` : '—' }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 积分兑换 -->
    <div class="card redeem-card">
      <div class="c-top">
        <h3>积分兑换时长</h3>
        <span class="pts mono">{{ ov?.points ?? 0 }} 积分</span>
      </div>
      <div class="r-row">
        <UISelect v-model="instId" :options="instOpts" placeholder="选择要续期的实例" style="flex:1" />
        <UIInput v-model.number="days" type="number" :min="1" :max="30" style="width:110px" />
        <span class="r-day text-dim">天</span>
        <UIButton :disabled="!canRedeem()" :loading="redeeming" @click="doRedeem">兑换（{{ days * (ov?.points_per_day ?? 100) }} 积分）</UIButton>
      </div>
      <p class="c-sub text-dim">兑换后立即为实例续期对应天数（到期时间顺延），每天消耗 {{ ov?.points_per_day ?? 100 }} 积分。</p>
    </div>
  </div>
</template>

<style scoped>
.page-head { margin-bottom: 20px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.card {
  border: 1px solid var(--line); border-radius: 16px; background: var(--panel);
  padding: 20px 22px; display: flex; flex-direction: column; gap: 12px;
}
.c-top { display: flex; align-items: center; justify-content: space-between; }
.c-top h3 { margin: 0; font-size: 15px; font-weight: 800; }
.pts { font-size: 15px; font-weight: 800; color: var(--primary-strong); }
.hl { color: var(--primary-strong); }
.c-desc { margin: 0; font-size: 12.5px; line-height: 1.8; }
.c-sub { margin: 0; font-size: 11.5px; }

/* 签到 */
.week { display: flex; gap: 10px; }
.wdot {
  flex: 1; height: 8px; border-radius: 999px; background: var(--bg);
  border: 1px solid var(--line); transition: all .3s;
}
.wdot.on { background: var(--primary); border-color: var(--primary); box-shadow: 0 0 8px -2px var(--primary); }
.ck-btn { width: 100%; }
.ck-btn:disabled { opacity: .65; }

/* 等级 */
.lv-tag {
  font-size: 12px; font-weight: 800; color: #fff; background: linear-gradient(135deg, var(--primary), var(--primary-deep, #0b5c4a));
  border-radius: 999px; padding: 3px 12px;
}
.lv-bar { height: 8px; border-radius: 999px; background: var(--bg); border: 1px solid var(--line); overflow: hidden; }
.lv-fill { height: 100%; background: linear-gradient(90deg, var(--primary), var(--primary-strong)); border-radius: 999px; transition: width .5s ease; }
.lv-list { display: flex; flex-direction: column; gap: 0; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.lv-row {
  display: flex; align-items: center; gap: 10px; padding: 7px 12px; font-size: 12px; background: var(--panel);
}
.lv-row + .lv-row { border-top: 1px solid var(--line); }
.lv-row.now { background: var(--primary-soft, #e8f6f1); }
.lv-l { font-weight: 800; width: 36px; color: var(--primary-strong); }
.lv-e { color: var(--text-2); flex: 1; }
.lv-d { font-weight: 700; }
.lv-row.now .lv-d { color: var(--primary-strong); }

/* 兑换 */
.redeem-card { margin-top: 16px; }
.r-row { display: flex; align-items: center; gap: 10px; }
.r-day { font-size: 13px; }
</style>
