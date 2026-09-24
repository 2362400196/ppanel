<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import ToastHost from './components/ui/ToastHost.vue'
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
            <i class="nav-icon box" />
            <span>仪表盘</span>
          </router-link>
          <router-link to="/shop" class="nav-item" :class="{ active: route.name === 'shop' }">
            <i class="nav-icon docker" />
            <span>商城</span>
          </router-link>
          <router-link to="/settings" class="nav-item" :class="{ active: route.name === 'settings' }">
            <i class="nav-icon nodes" />
            <span>设置</span>
          </router-link>
        </template>

        <!-- 管理员视角：专注于管理（商品 / 用户 / 实例 / 节点 / Docker） -->
        <template v-else>
          <router-link to="/admin" class="nav-item" :class="{ active: route.name === 'admin' }">
            <i class="nav-icon shield" />
            <span>商品管理</span>
          </router-link>
          <router-link to="/users" class="nav-item" :class="{ active: route.name === 'users' }">
            <i class="nav-icon box" />
            <span>用户管理</span>
          </router-link>
          <router-link to="/manage" class="nav-item" :class="{ active: route.name === 'manage' }">
            <i class="nav-icon docker" />
            <span>实例管理</span>
          </router-link>
          <router-link to="/nodes" class="nav-item" :class="{ active: route.name === 'nodes' }">
            <i class="nav-icon nodes" />
            <span>节点管理</span>
          </router-link>
          <router-link to="/docker" class="nav-item" :class="{ active: route.name === 'docker' }">
            <i class="nav-icon docker" />
            <span>Docker 管理</span>
          </router-link>
          <router-link to="/settings" class="nav-item" :class="{ active: route.name === 'settings' }">
            <i class="nav-icon nodes" />
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
.nav-icon { width: 15px; height: 15px; border: 1.6px solid currentColor; border-radius: 4px; }
.nav-icon.shield { border-radius: 4px 4px 8px 8px; }
.nav-icon.docker { border-radius: 50%; }
.nav-icon.nodes { border-radius: 50%; width: 9px; height: 9px; margin: 3px; box-shadow: 4px 0 0 -1.2px currentColor, -4px 0 0 -1.2px currentColor; }

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
