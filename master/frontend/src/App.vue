<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import ToastHost from './components/ui/ToastHost.vue'
import TaskTerminal from './components/TaskTerminal.vue'
import UITag from './components/ui/UITag.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const bare = computed(() => route.meta.bare)

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
          <router-link to="/" class="nav-item" :class="{ active: route.name === 'instances' || route.name === 'instance-detail' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>
            <span>仪表盘</span>
          </router-link>
          <router-link to="/shop" class="nav-item" :class="{ active: route.name === 'shop' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><path d="M6 7h12l1.5 13.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1L6 7z" /><path d="M9 10V6a3 3 0 0 1 6 0v4" /></svg>
            <span>商城</span>
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
          <router-link to="/settings" class="nav-item" :class="{ active: route.name === 'settings' }">
            <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.09a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.09a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
            <span>设置</span>
          </router-link>
        </template>
      </nav>

      <div class="side-foot" v-if="auth.user">
        <div class="user-row">
          <span class="avatar">{{ auth.user.username.slice(0, 1).toUpperCase() }}</span>
          <div class="user-meta">
            <span class="user-name">{{ auth.user.username }}</span>
            <UITag :tone="auth.isAdmin ? 'warn' : 'primary'">
              {{ auth.isAdmin ? '管理员' : '用户' }}
            </UITag>
          </div>
        </div>
        <button class="logout" @click="logout">退出登录</button>
      </div>
    </aside>

    <main class="main">
      <router-view v-slot="{ Component }">
        <Transition name="fade-slide" mode="out-in">
          <component :is="Component" />
        </Transition>
      </router-view>
    </main>
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

.side-foot { border-top: 1px solid rgba(255,255,255,.08); padding-top: 14px; }
.user-row { display: flex; align-items: center; gap: 10px; padding: 0 8px 10px; }
.avatar {
  width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
  background: rgba(47,181,159,.2); color: var(--primary);
  display: flex; align-items: center; justify-content: center; font-weight: 700;
}
.user-meta { display: flex; flex-direction: column; gap: 3px; }
.user-name { color: #fff; font-size: 13px; font-weight: 600; }
.user-meta :deep(.ui-tag) { height: 18px; font-size: 11px; padding: 0 7px; align-self: flex-start; }
.logout {
  width: 100%; height: 32px; border: none; border-radius: 8px; cursor: pointer;
  background: transparent; color: var(--sidebar-text); font-size: 13px; font-family: inherit;
  transition: all .15s ease;
}
.logout:hover { color: #fff; background: rgba(224,82,96,.18); }

.main { flex: 1; overflow-y: auto; padding: 28px 32px; }

@media (max-width: 720px) {
  .layout { flex-direction: column; }
  .sidebar { width: 100%; flex-direction: row; align-items: center; padding: 10px 14px; }
  .nav { flex-direction: row; flex-wrap: wrap; }
  .side-foot { border: none; padding: 0; }
  .user-row { padding: 0; }
  .main { padding: 18px 16px; }
}
</style>
