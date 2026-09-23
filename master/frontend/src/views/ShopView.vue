<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITag from '../components/ui/UITag.vue'
import UISelect from '../components/ui/UISelect.vue'

const router = useRouter()
const images = ref([])
const image = ref('')
const buying = ref('')

// 商城套餐（MVP：无支付，开通即创建，到期 30 天）
const plans = [
  { key: 'lite', name: '轻量型', desc: '个人小站 / 学习练手', cpu: 1, mem: 512, disk: 2048, price: '¥5', per: '/月' },
  { key: 'std', name: '标准型', desc: '常规 Web 应用 / 机器人', cpu: 1, mem: 1024, disk: 5120, price: '¥12', per: '/月' },
  { key: 'pro', name: '进阶型', desc: '高并发服务 / 数据处理', cpu: 2, mem: 2048, disk: 10240, price: '¥25', per: '/月' },
]

onMounted(async () => {
  try {
    const { data } = await api.get('/images')
    images.value = data
    image.value = data[0] || 'python:3.11-slim'
  } catch { image.value = 'python:3.11-slim' }
})

async function buy(plan) {
  buying.value = plan.key
  try {
    const suffix = Math.random().toString(36).slice(2, 6)
    const expire = new Date(Date.now() + 30 * 86400000).toISOString()
    const { data } = await api.post('/instances', {
      name: `面板-${suffix}`,
      image: image.value,
      cpu_limit: plan.cpu,
      mem_limit: plan.mem,
      disk_quota: plan.disk,
      expire_at: expire,
    })
    toastOk(`已开通「${data.name}」，有效期 30 天`)
    router.push('/')
  } catch (e) {
    toastErr(errText(e))
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
        <p class="page-sub">选择套餐开通独立面板，每个面板 = 一个专属 Python 容器</p>
      </div>
      <div class="img-pick">
        <span class="img-label">环境版本</span>
        <UISelect v-model="image" :options="images.map(i => ({ value: i, label: i }))" />
      </div>
    </div>

    <div class="plans">
      <div v-for="p in plans" :key="p.key" class="plan card">
        <div class="plan-top">
          <UITag tone="primary">{{ p.name }}</UITag>
          <span class="mono price">{{ p.price }}<i class="per">{{ p.per }}</i></span>
        </div>
        <p class="desc">{{ p.desc }}</p>
        <div class="specs">
          <div class="spec"><span class="k">CPU</span><span class="v">{{ p.cpu }} 核</span></div>
          <div class="spec"><span class="k">内存</span><span class="v">{{ p.mem }}MB</span></div>
          <div class="spec"><span class="k">磁盘</span><span class="v">{{ p.disk / 1024 }}GB</span></div>
          <div class="spec"><span class="k">到期</span><span class="v">30 天</span></div>
        </div>
        <UIButton class="buy" :loading="buying === p.key" @click="buy(p)">立即开通</UIButton>
      </div>
    </div>

    <p class="note text-dim">开通后可在仪表盘进入面板管理容器，到期前请续期。</p>
  </div>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 22px; }
.img-pick { display: flex; align-items: center; gap: 8px; }
.img-label { font-size: 13px; color: var(--text-dim); }

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
</style>
