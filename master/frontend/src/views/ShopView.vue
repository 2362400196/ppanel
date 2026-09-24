<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITag from '../components/ui/UITag.vue'

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

async function buy(plan) {
  buying.value = plan.id
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
      node_id: plan.node_id || undefined,  // 商品绑定节点则在该节点开通
    })
    toastOk(`已开通「${data.name}」，有效期 ${plan.days} 天`)
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
        <p class="page-sub">选择套餐开通独立面板，运行环境由套餐决定，开通即用</p>
      </div>
    </div>

    <div v-if="!loading && !plans.length" class="empty">商品暂未上架，请稍后再来。</div>

    <div v-else class="plans">
      <div v-for="p in plans" :key="p.id" class="plan card">
        <div class="plan-top">
          <UITag tone="primary">{{ p.name }}</UITag>
          <span class="mono price">{{ priceText(p) }}<i class="per">/ {{ p.days }} 天</i></span>
        </div>
        <p class="desc">{{ p.desc }}</p>
        <div class="specs">
          <div class="spec"><span class="k">环境</span><span class="v mono">{{ p.image || 'python:3.11-slim' }}</span></div>
          <div class="spec"><span class="k">CPU</span><span class="v">{{ p.cpu }} 核</span></div>
          <div class="spec"><span class="k">内存</span><span class="v">{{ p.mem }}MB</span></div>
          <div class="spec"><span class="k">磁盘</span><span class="v">{{ p.disk / 1024 }}GB</span></div>
          <div class="spec"><span class="k">到期</span><span class="v">{{ p.days }} 天</span></div>
        </div>
        <UIButton class="buy" :loading="buying === p.id" @click="buy(p)">立即开通</UIButton>
      </div>
    </div>

    <p class="note text-dim">开通后可在仪表盘进入面板管理容器，到期前请续期。</p>
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
</style>
