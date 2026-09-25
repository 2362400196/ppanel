<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, errText } from '../../api/client'
import { toastErr, toastOk } from '../../components/ui/toast'
import UIButton from '../../components/ui/UIButton.vue'

const props = defineProps({ instance: Object })
const emit = defineEmits(['changed'])

// 独立面板令牌：容器操作全部在被控 /panel 完成，主控只负责发令牌
const panelToken = ref('')
const panelUrl = ref('')
const panelBusy = ref(false)

// 资源用量（被控 stats：CPU/内存/磁盘/流量）
const stats = ref(null)
let timer
async function loadStats() {
  try {
    stats.value = await api.get(`/instances/${props.instance.id}/stats`).then(r => r.data)
  } catch { stats.value = null }
}
onMounted(() => { loadStats(); timer = setInterval(loadStats, 15000) })
onUnmounted(() => clearInterval(timer))

const pct = (used, limit) => (!limit ? 0 : Math.min(100, used / limit * 100))

const meters = computed(() => {
  const s = stats.value
  if (!s) return []
  const list = [
    { k: 'CPU 占用', pct: Math.min(100, s.cpu_percent || 0),
      v: `${(s.cpu_percent || 0).toFixed(1)}% / ${props.instance.cpu_limit} 核` },
    { k: '内存占用', pct: pct(s.mem_usage_mb || 0, s.mem_limit_mb),
      v: `${(s.mem_usage_mb || 0).toFixed(0)} / ${s.mem_limit_mb || props.instance.mem_limit} MB` },
  ]
  if (s.disk_used_mb != null) {
    list.push({ k: '磁盘用量', pct: pct(s.disk_used_mb, props.instance.disk_quota),
      v: `${s.disk_used_mb.toFixed(0)} MB / ${(props.instance.disk_quota / 1024).toFixed(0)} GB` })
  }
  if (s.traffic_gb) {
    list.push({ k: '本月流量', pct: pct(s.traffic_used_mb || 0, s.traffic_gb * 1024),
      v: `${((s.traffic_used_mb || 0) / 1024).toFixed(2)} / ${s.traffic_gb} GB` })
  }
  return list
})

async function issuePanelToken(reset) {
  panelBusy.value = true
  try {
    const { data } = await api.post(`/instances/${props.instance.id}/panel-token?reset=${reset ? 1 : 0}`)
    panelToken.value = data.panel_token
    panelUrl.value = data.panel_url
    toastOk(reset ? '面板令牌已重置，旧链接立即失效' : '面板令牌已生成')
    emit('changed')
  } catch (e) {
    toastErr(errText(e))
  } finally {
    panelBusy.value = false
  }
}

async function copyPanelLink() {
  const link = `${panelUrl.value}?t=${panelToken.value}`
  try {
    await navigator.clipboard.writeText(link)
    toastOk('一键登录链接已复制')
  } catch {
    toastErr('复制失败，请手动复制')
  }
}
</script>

<template>
  <div class="ov-wrap">
    <div class="card panel-card">
      <div class="card-head">
        <span class="card-title">资源用量</span>
        <span class="tip">每 15 秒自动刷新；流量为自然月累计，次月清零</span>
      </div>
      <div class="meters">
        <div v-for="m in meters" :key="m.k" class="meter-row">
          <span class="k">{{ m.k }}</span>
          <div class="bar"><i :class="{ hot: m.pct > 85 }" :style="{ width: m.pct + '%' }"></i></div>
          <span class="v mono">{{ m.v }}</span>
        </div>
        <p v-if="!meters.length" class="hint">实例不在线或数据加载中…</p>
      </div>
    </div>

    <div class="card panel-card">
      <div class="card-head">
        <span class="card-title">独立面板</span>
        <span class="tip">容器操作全部在独立面板完成，主控只负责开通与发令牌</span>
      </div>
      <p class="hint" style="margin-bottom:12px">
        生成面板令牌后，把一键登录链接发给使用者，即可在浏览器中独立管理这个容器：
        启停重启、文件管理、实时日志、依赖安装、启动命令——无需登录主控，也可脱离主控单独使用。
      </p>
      <template v-if="panelToken">
        <div class="p-link mono">{{ panelUrl }}?t={{ panelToken.slice(0, 6) }}****</div>
        <div class="panel-btns">
          <UIButton size="sm" @click="copyPanelLink">复制一键登录链接</UIButton>
          <UIButton size="sm" type="ghost" :loading="panelBusy" @click="issuePanelToken(true)">重置令牌</UIButton>
          <a :href="`${panelUrl}?t=${panelToken}`" target="_blank">
            <UIButton size="sm" type="ghost">打开面板</UIButton>
          </a>
        </div>
      </template>
      <template v-else>
        <UIButton size="sm" :loading="panelBusy" @click="issuePanelToken(false)">生成面板令牌</UIButton>
      </template>
    </div>
  </div>
</template>

<style scoped>
.ov-wrap { display: flex; flex-direction: column; gap: 16px; }
.panel-card { padding: 20px; }
.card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.card-title { font-size: 14px; font-weight: 700; }
.tip { font-size: 11px; color: var(--text-dim); }
.hint { font-size: 13px; color: var(--text-dim); line-height: 1.7; }
.meters { display: flex; flex-direction: column; gap: 10px; }
.meter-row { display: grid; grid-template-columns: 72px 1fr auto; align-items: center; gap: 12px; }
.meter-row .k { font-size: 12.5px; color: var(--text-2); }
.meter-row .v { font-size: 12px; color: var(--text-2); }
.bar { height: 8px; background: var(--bg-2, #eef1f3); border-radius: 99px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--primary); border-radius: 99px; transition: width .5s ease; }
.bar i.hot { background: #e05a4e; }
.mono { font-family: ui-monospace, Consolas, monospace; }
.p-link {
  font-size: 12px; color: var(--text-2); background: var(--primary-soft);
  border: 1px dashed var(--line); border-radius: 8px; padding: 8px 10px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 10px;
}
.panel-btns { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
