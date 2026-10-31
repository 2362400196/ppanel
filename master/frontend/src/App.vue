<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { api } from './api/client'
import ToastHost from './components/ui/ToastHost.vue'
import TaskTerminal from './components/TaskTerminal.vue'
import UITag from './components/ui/UITag.vue'
import AiAssistant from './components/AiAssistant.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const bare = computed(() => route.meta.bare)

// 侧边栏「告警中心」未处理数量徽标：每分钟刷新一次，失败静默
const alertCount = ref(0)
let alertTimer = null

async function loadAlertCount() {
  if (!auth.isAdmin) {
    alertCount.value = 0
    return
  }
  try {
    const { data } = await api.get('/admin/alerts/summary')
    alertCount.value = data.total || 0
  } catch {
    alertCount.value = 0
  }
}
watch(() => auth.isAdmin, loadAlertCount)

// 右上角用户下拉：点外部/切页自动收起
const menuOpen = ref(false)
function onDocClick() { menuOpen.value = false }

// ---------- 站内信（到期提醒 / 暂停通知 / 额度告警） ----------
const noticeOpen = ref(false)
const notices = ref([])
const unreadCount = computed(() => notices.value.filter(n => !n.read).length)

async function loadNotices() {
  if (!auth.user) return
  try {
    const { data } = await api.get('/notices?limit=20')
    notices.value = data
  } catch { /* 静默：通知拉取失败不打扰 */ }
}

async function readOne(n) {
  if (n.read) return
  try { await api.post(`/notices/${n.id}/read`); n.read = true } catch { /* 静默 */ }
}

async function readAll() {
  try {
    await api.post('/notices/read-all')
    notices.value.forEach(n => { n.read = true })
  } catch { /* 静默 */ }
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  loadAlertCount()
  loadNotices()
  alertTimer = setInterval(() => { loadAlertCount(); loadNotices() }, 60000)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  if (alertTimer) clearInterval(alertTimer)
})
watch(() => route.fullPath, () => { menuOpen.value = false })

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <ToastHost />
  <TaskTerminal />
  <router-view v-if="bare" />

  <div v-else class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="logo">P</span>
        <span class="brand-name">PPanel</span>
      </div>

      <nav class="nav">
        <!-- 用户视角：仪表盘 / 商城 / 设置 -->
        <template v-if="!auth.isAdmin">
          <router-link to="/instances" class="nav-item" :class="{ active: route.name === 'instances' || route.name === 'instance-detail' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>
            <span>仪表盘</span>
          </router-link>
          <router-link to="/shop" class="nav-item" :class="{ active: route.name === 'shop' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M6 7h12l1.5 13.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1L6 7z" /><path d="M9 10V6a3 3 0 0 1 6 0v4" /></svg>
            <span>商城</span>
          </router-link>
          <router-link to="/wallet" class="nav-item" :class="{ active: route.name === 'wallet' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><rect x="2.5" y="6" width="19" height="13" rx="2.5" /><path d="M2.5 10h19" /><circle cx="17" cy="14.5" r="1.1" /></svg>
            <span>钱包</span>
          </router-link>
          <router-link to="/tickets" class="nav-item" :class="{ active: route.name === 'tickets' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H9l-4.2 3.6c-.4.3-.8 0-.8-.4V5.5z" /><path d="M8 9h8M8 12.5h5" /></svg>
            <span>工单</span>
          </router-link>
          <router-link to="/rewards" class="nav-item" :class="{ active: route.name === 'rewards' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M12 21s-7-4.6-9.3-9A5.6 5.6 0 0 1 12 6.2 5.6 5.6 0 0 1 21.3 12C19 16.4 12 21 12 21z" /></svg>
            <span>福利</span>
          </router-link>
          <router-link to="/settings" class="nav-item" :class="{ active: route.name === 'settings' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.09a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.09a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
            <span>设置</span>
          </router-link>
        </template>

        <!-- 管理员视角：仪表盘 + 管理 -->
        <template v-else>
          <router-link to="/dashboard" class="nav-item" :class="{ active: route.name === 'dashboard' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>
            <span>仪表盘</span>
          </router-link>
          <router-link to="/users" class="nav-item" :class="{ active: route.name === 'users' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2" /><circle cx="10" cy="7" r="4" /><path d="M21 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></svg>
            <span>用户管理</span>
          </router-link>
          <router-link to="/nodes" class="nav-item" :class="{ active: route.name === 'nodes' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" /><line x1="8.6" y1="10.5" x2="15.4" y2="6.5" /><line x1="8.6" y1="13.5" x2="15.4" y2="17.5" /></svg>
            <span>节点管理</span>
          </router-link>
          <router-link to="/alerts" class="nav-item" :class="{ active: route.name === 'alerts' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></svg>
            <span>告警中心</span>
            <span v-if="alertCount" class="nav-badge">{{ alertCount > 99 ? '99+' : alertCount }}</span>
          </router-link>
          <router-link to="/backup" class="nav-item" :class="{ active: route.name === 'backup' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></svg>
            <span>备份中心</span>
          </router-link>
          <router-link to="/manage" class="nav-item" :class="{ active: route.name === 'manage' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="7" rx="2" /><rect x="2" y="14" width="20" height="7" rx="2" /><line x1="6" y1="6.5" x2="6.01" y2="6.5" /><line x1="6" y1="17.5" x2="6.01" y2="17.5" /></svg>
            <span>实例管理</span>
          </router-link>
          <router-link to="/docker" class="nav-item" :class="{ active: route.name === 'docker' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M21 8.5 12 3 3 8.5v7L12 21l9-5.5v-7z" /><line x1="3" y1="8.5" x2="12" y2="13.5" /><line x1="21" y1="8.5" x2="12" y2="13.5" /><line x1="12" y1="13.5" x2="12" y2="21" /></svg>
            <span>Docker 管理</span>
          </router-link>
          <router-link to="/admin" class="nav-item" :class="{ active: route.name === 'admin' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0l-7-7A2 2 0 0 1 3 12.2V5a2 2 0 0 1 2-2h7.2a2 2 0 0 1 1.4.6l7 7a2 2 0 0 1 0 2.8z" /><circle cx="7.5" cy="7.5" r="1.2" /></svg>
            <span>商品管理</span>
          </router-link>
          <router-link to="/orders" class="nav-item" :class="{ active: route.name === 'orders' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M6 2h12l1.5 5H4.5L6 2z" /><path d="M4.5 7h15V20a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2V7z" /><path d="M9 11.5h6" /></svg>
            <span>订单支付</span>
          </router-link>
          <router-link to="/admin-tickets" class="nav-item" :class="{ active: route.name === 'admin-tickets' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H9l-4.2 3.6c-.4.3-.8 0-.8-.4V5.5z" /><path d="M8 9h8M8 12.5h5" /></svg>
            <span>工单处理</span>
          </router-link>
          <router-link to="/admin-coupons" class="nav-item" :class="{ active: route.name === 'admin-coupons' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M3 9V7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2a3 3 0 0 0 0 6v2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-2a3 3 0 0 0 0-6z" /><path d="M13 5v2.5M13 11v2M13 16.5V19" /></svg>
            <span>优惠券</span>
          </router-link>
          <router-link to="/admin-rewards" class="nav-item" :class="{ active: route.name === 'admin-rewards' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="8" r="6" /><path d="M15.5 12.9 17 22l-5-3-5 3 1.5-9.1" /></svg>
            <span>福利设置</span>
          </router-link>
          <router-link to="/admin-upgrade" class="nav-item" :class="{ active: route.name === 'admin-upgrade' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M12 3v12" /><path d="m7 10 5-5 5 5" /><path d="M5 21h14" /></svg>
            <span>系统升级</span>
          </router-link>
          <router-link to="/settings" class="nav-item" :class="{ active: route.name === 'settings' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.09a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.09a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
            <span>设置</span>
          </router-link>
        </template>
      </nav>
    </aside>

    <main class="main">
      <header class="topbar" v-if="auth.user" @click="noticeOpen = false; menuOpen = false">
        <div class="top-right" @click.stop>
          <!-- 站内信铃铛 -->
          <div class="bell-wrap">
            <button class="bell-btn" :class="{ open: noticeOpen }" @click="noticeOpen = !noticeOpen" title="通知">
              <svg viewBox="0 0 24 24"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></svg>
              <span v-if="unreadCount" class="bell-badge">{{ unreadCount > 9 ? '9+' : unreadCount }}</span>
            </button>
            <Transition name="dd">
              <div class="notice-dd" v-if="noticeOpen">
                <div class="notice-head">
                  <span>通知</span>
                  <button v-if="unreadCount" class="notice-readall" @click="readAll">全部已读</button>
                </div>
                <div class="notice-list">
                  <button v-for="n in notices" :key="n.id" class="notice-item"
                          :class="[n.level, { unread: !n.read }]" @click="readOne(n)">
                    <span class="notice-title">{{ n.title }}</span>
                    <span class="notice-content">{{ n.content }}</span>
                    <span class="notice-time">{{ (n.created_at || '').slice(0, 16).replace('T', ' ') }}</span>
                  </button>
                  <div v-if="!notices.length" class="notice-empty">暂无通知</div>
                </div>
              </div>
            </Transition>
          </div>
          <button class="user-chip" :class="{ open: menuOpen }" @click="menuOpen = !menuOpen">
            <span class="avatar">{{ auth.user.username.slice(0, 1).toUpperCase() }}</span>
            <span class="chip-name">{{ auth.user.username }}</span>
            <UITag :tone="auth.isAdmin ? 'warn' : 'primary'">{{ auth.isAdmin ? '管理员' : '用户' }}</UITag>
            <svg class="chev" :class="{ up: menuOpen }" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9" /></svg>
          </button>
          <Transition name="dd">
            <div class="user-dd" v-if="menuOpen">
              <button class="dd-item" @click="logout">
                <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" /></svg>
                <span>退出登录</span>
              </button>
            </div>
          </Transition>
        </div>
      </header>
      <div class="main-body">
        <router-view v-slot="{ Component }">
          <Transition name="fade-slide" mode="out-in">
            <component :is="Component" />
          </Transition>
        </router-view>
      </div>
    </main>
    <!-- 全局 AI 助手（右下角悬浮球，登录即可用；管理员/普通用户工具自动分流） -->
    <AiAssistant v-if="auth.user" />
  </div>
</template>

<style scoped>
.layout { display: flex; height: 100vh; }

.sidebar {
  width: 210px; flex-shrink: 0; display: flex; flex-direction: column;
  background: var(--sidebar-bg); padding: 20px 14px;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 4px 10px 20px; }
.logo {
  width: 34px; height: 34px; border-radius: 10px;
  background: linear-gradient(135deg, var(--primary), var(--primary-deep));
  color: #fff; font-weight: 800; font-size: 17px; font-family: var(--font-mono);
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 14px rgba(47,181,159,.4);
}
.brand-name { color: #fff; font-size: 17px; font-weight: 700; letter-spacing: 1px; }

.nav { display: flex; flex-direction: column; gap: 4px; flex: 1; }
.nav-item {
  display: flex; align-items: center; gap: 10px; height: 40px; padding: 0 12px;
  border-radius: 9px; color: var(--sidebar-text); font-size: 14px;
  transition: all .15s ease; position: relative;
}
.nav-item:hover { color: #fff; background: rgba(255,255,255,.06); }
.nav-item.active { color: #fff; background: rgba(47,181,159,.16); font-weight: 600; }
.nav-item.active::before {
  content: ''; position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px;
  border-radius: 2px; background: var(--primary);
}
.nav-icon {
  width: 16px; height: 16px; flex-shrink: 0;
  fill: none; stroke: currentColor; stroke-width: 1.8;
  stroke-linecap: round; stroke-linejoin: round;
}
.nav-badge {
  margin-left: auto; min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: 9px; background: var(--danger); color: #fff;
  font-size: 11px; font-weight: 500; line-height: 18px; text-align: center;
}

.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.main-body { flex: 1; overflow-y: auto; padding: 28px 32px; }

/* 顶栏：右上角用户区 */
/* 站内信铃铛 */
.bell-wrap { position: relative; }
.bell-btn {
  position: relative; width: 33px; height: 33px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--panel); cursor: pointer;
  display: flex; align-items: center; justify-content: center; transition: all .18s ease;
  box-shadow: var(--shadow);
}
.bell-btn svg { width: 15px; height: 15px; fill: none; stroke: var(--text-2); stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.bell-btn:hover, .bell-btn.open { background: var(--primary-soft-2); border-color: var(--primary); }
.bell-btn:hover svg, .bell-btn.open svg { stroke: var(--primary-strong); }
.bell-badge {
  position: absolute; top: -4px; right: -4px; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; background: var(--danger); color: #fff;
  font-size: 10px; font-weight: 600; line-height: 16px; text-align: center;
}
.notice-dd {
  position: absolute; right: 0; top: calc(100% + 7px); width: 330px;
  border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
  background: var(--panel); box-shadow: var(--shadow-lg); z-index: 60;
}
.notice-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px; border-bottom: 1px solid var(--line);
  font-size: 12.5px; font-weight: 700; color: var(--text);
}
.notice-readall {
  border: none; background: transparent; cursor: pointer; font-family: inherit;
  color: var(--primary-strong); font-size: 12px; font-weight: 600;
}
.notice-list { max-height: 340px; overflow-y: auto; }
.notice-item {
  display: flex; flex-direction: column; gap: 3px; width: 100%; text-align: left;
  padding: 9px 12px; border: none; border-bottom: 1px solid var(--line);
  background: transparent; cursor: pointer; font-family: inherit;
}
.notice-item:last-child { border-bottom: none; }
.notice-item:hover { background: var(--primary-soft); }
.notice-item.unread { background: color-mix(in srgb, var(--primary-soft) 45%, transparent); }
.notice-item.unread .notice-title::before {
  content: ''; display: inline-block; width: 6px; height: 6px; border-radius: 3px;
  background: var(--primary); margin-right: 6px; vertical-align: 2px;
}
.notice-item.critical.unread .notice-title::before { background: var(--danger); }
.notice-title { font-size: 12.5px; font-weight: 600; color: var(--text); }
.notice-content {
  font-size: 11.5px; color: var(--text-2); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.notice-time { font-size: 10.5px; color: var(--text-dim); }
.notice-empty { padding: 26px 0; text-align: center; font-size: 12px; color: var(--text-dim); }
.avatar {
  width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0;
  background: var(--primary-soft); color: var(--primary-strong);
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 11px;
}
.topbar {
  height: 50px; flex-shrink: 0; display: flex; align-items: center; justify-content: flex-end;
  padding: 0 24px; border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--panel) 82%, transparent);
  backdrop-filter: blur(10px); position: relative; z-index: 40;
}
.top-right { position: relative; display: flex; align-items: center; gap: 10px; }
.user-chip {
  display: flex; align-items: center; gap: 7px; height: 33px; padding: 0 10px;
  border: 1px solid var(--line); border-radius: 999px; cursor: pointer;
  background: var(--panel); font-family: inherit; transition: all .18s ease;
  box-shadow: var(--shadow);
}
.user-chip:hover, .user-chip.open { background: var(--primary-soft-2); border-color: var(--primary); }
.chip-name { color: var(--text); font-size: 12.5px; font-weight: 600; }
.user-chip :deep(.ui-tag) { height: 16px; font-size: 10.5px; padding: 0 6px; }
.chev {
  width: 13px; height: 13px; fill: none; stroke: var(--text-dim); stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; transition: transform .2s ease;
}
.chev.up { transform: rotate(180deg); stroke: var(--primary-strong); }
/* 下拉与胶囊等宽右对齐：紧凑单卡，只放退出登录 */
.user-dd {
  position: absolute; right: 0; top: calc(100% + 7px); width: max-content; min-width: 100%;
  border: 1px solid var(--line); border-radius: 10px; padding: 5px;
  background: var(--panel);
  box-shadow: var(--shadow-lg);
}
.dd-item {
  display: flex; align-items: center; gap: 8px; width: 100%; height: 30px; padding: 0 10px;
  border: none; border-radius: 7px; cursor: pointer; background: transparent;
  color: var(--text-2); font-size: 12.5px; font-family: inherit; transition: all .14s ease; white-space: nowrap;
}
.dd-item svg { width: 13.5px; height: 13.5px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.dd-item:hover { background: var(--danger-soft); color: var(--danger); }
.dd-enter-active, .dd-leave-active { transition: all .16s ease; }
.dd-enter-from, .dd-leave-to { opacity: 0; transform: translateY(-5px) scale(.98); }

@media (max-width: 720px) {
  .layout { flex-direction: column; }
  .sidebar { width: 100%; flex-direction: row; align-items: center; padding: 10px 14px; }
  .nav { flex-direction: row; flex-wrap: wrap; }
  .main-body { padding: 18px 16px; }
  .topbar { height: 48px; padding: 0 14px; }
}
</style>
