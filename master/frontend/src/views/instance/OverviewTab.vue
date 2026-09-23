<script setup>
import { ref } from 'vue'
import { api, errText } from '../../api/client'
import { toastErr, toastOk } from '../../components/ui/toast'
import UIButton from '../../components/ui/UIButton.vue'

const props = defineProps({ instance: Object })
const emit = defineEmits(['changed'])

// 独立面板令牌：容器操作全部在被控 /panel 完成，主控只负责发令牌
const panelToken = ref('')
const panelUrl = ref('')
const panelBusy = ref(false)

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
.p-link {
  font-size: 12px; color: var(--text-2); background: var(--primary-soft);
  border: 1px dashed var(--line); border-radius: 8px; padding: 8px 10px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 10px;
}
.panel-btns { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
