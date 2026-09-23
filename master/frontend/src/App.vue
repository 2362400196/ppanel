<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api/client'
import { useAuthStore } from './stores/auth'
import ToastHost from './components/ui/ToastHost.vue'
import UITag from './components/ui/UITag.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const bare = computed(() => route.meta.bare)

// 侧栏实例列表：一个被控 = 一个容器分组，一个实例 = 一个面板
const instances = ref([])
const collapsed = ref(localStorage.getItem('ppanel_inst_folded') === '1')
const foldedNodes = ref({})
let timer

async function loadInstances() {
  try {
    instances.value = (await api.get('/instances')).data
  } catch { /* 静默重试 */ }
}

function toggleFold() {
  collapsed.value = !collapsed.value
  localStorage.setItem('ppanel_inst_folded', collapsed.value ? '1' : '0')
}

const nodeGroups = computed(() => {
  const m = new Map()
  for (const i of instances.value) {
    const key = i.node_id ?? 0
    if (!m.has(key)) m.set(key, { id: key, name: i.node_name || `节点 ${key}`, list: [] })
    m.get(key).list.push(i)
  }
  return [...m.values()]
})

const isNodeFolded = id => !!foldedNodes.value[id]

function toggleNode(id) {
  foldedNodes.value = { ...foldedNodes.value, [id]: !foldedNodes.value[id] }
}

onMounted(() => {
  loadInstances()
  timer = setInterval(loadInstances, 10000)
})
onUnmounted(() => clearInterval(timer))

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

        <!-- 管理员视角：按被控节点分组，一个被控 = 一个容器分组 -->
        <template v-else>
          <div class="nav-group">
            <router-link to="/" class="nav-item group-head" :class="{ active: !['admin', 'docker', 'nodes'].includes(route.name) }">
              <i class="nav-icon box" />
              <span>我的实例</span>
              <i class="chev" :class="{ folded: collapsed }" @click.prevent.stop="toggleFold" />
            </router-link>
            <div class="sub-list" :class="{ folded: collapsed }">
              <div class="sub-inner">
                <div v-for="g in nodeGroups" :key="g.id" class="node-group">
                  <button class="node-head" @click="toggleNode(g.id)">
                    <i class="chev sm" :class="{ folded: isNodeFolded(g.id) }" />
                    <span class="node-name">{{ g.name }}</span>
                    <span class="node-count">{{ g.list.length }}</span>
                  </button>
                  <div class="node-list" :class="{ folded: isNodeFolded(g.id) }">
                    <div class="node-inner">
                      <router-link
                        v-for="i in g.list" :key="i.id"
                        :to="`/instances/${i.id}`"
                        class="sub-item" :class="{ active: route.name === 'instance-detail' && route.params.id === i.id }"
                      >
                        <i class="mini-dot" :class="i.status" />
                        <span class="sub-name">{{ i.name }}</span>
                      </router-link>
                    </div>
                  </div>
                </div>
                <div v-if="!instances.length" class="sub-empty">暂无实例</div>
              </div>
            </div>
          </div>
          <router-link to="/shop" class="nav-item" :class="{ active: route.name === 'shop' }">
            <i class="nav-icon docker" />
            <span>商城</span>
          </router-link>
          <router-link v-if="auth.isAdmin" to="/nodes" class="nav-item" :class="{ active: route.name === 'nodes' }">
            <i class="nav-icon nodes" />
            <span>节点管理</span>
          </router-link>
          <router-link v-if="auth.isAdmin" to="/admin" class="nav-item" :class="{ active: route.name === 'admin' }">
            <i class="nav-icon shield" />
            <span>管理后台</span>
          </router-link>
          <router-link v-if="auth.isAdmin" to="/docker" class="nav-item" :class="{ active: route.name === 'docker' }">
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

/* 实例分组（一个容器 = 一个面板） */
.group-head .chev { margin-left: auto; }
.chev {
  width: 7px; height: 7px; flex-shrink: 0;
  border-right: 1.6px solid currentColor; border-bottom: 1.6px solid currentColor;
  transform: rotate(45deg) translateY(-2px);
  transition: transform .25s ease; opacity: .55;
}
.chev:hover { opacity: 1; }
.chev.folded { transform: rotate(-45deg) translateY(-1px); }

/* 节点分组（一个被控 = 一个容器） */
.node-group { display: flex; flex-direction: column; }
.node-head {
  display: flex; align-items: center; gap: 7px; height: 26px; padding: 0 8px;
  border: none; background: transparent; cursor: pointer; font-family: inherit;
  font-size: 11px; font-weight: 700; color: rgba(255,255,255,.5);
  border-radius: 6px; transition: all .15s ease; text-align: left;
  letter-spacing: .5px;
}
.node-head:hover { color: #fff; background: rgba(255,255,255,.05); }
.chev.sm { width: 5px; height: 5px; opacity: .7; }
.node-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-count {
  margin-left: auto; font-size: 10px; font-weight: 600; font-family: var(--font-mono);
  background: rgba(255,255,255,.08); color: rgba(255,255,255,.5);
  min-width: 17px; height: 15px; padding: 0 4px; border-radius: 8px;
  display: inline-flex; align-items: center; justify-content: center;
}
.node-list {
  display: grid; grid-template-rows: 1fr;
  transition: grid-template-rows .25s ease;
}
.node-list.folded { grid-template-rows: 0fr; }
.node-inner {
  overflow: hidden; min-height: 0;
  display: flex; flex-direction: column; gap: 1px;
  padding-left: 9px; margin-left: 10px;
  border-left: 1px solid rgba(255,255,255,.06);
}
.node-list.folded .node-inner { overflow: hidden; }

.sub-list {
  display: grid; grid-template-rows: 1fr;
  transition: grid-template-rows .28s ease;
  margin: 2px 0 2px 14px; padding-left: 10px;
  border-left: 1px solid rgba(255,255,255,.08);
}
.sub-list.folded { grid-template-rows: 0fr; }
.sub-inner {
  overflow: hidden; min-height: 0;
  display: flex; flex-direction: column; gap: 1px;
  max-height: 264px; overflow-y: auto;
  scrollbar-width: none;
}
.sub-inner::-webkit-scrollbar { display: none; }
.sub-list.folded .sub-inner { overflow: hidden; }
.sub-item {
  display: flex; align-items: center; gap: 8px; height: 30px; padding: 0 10px;
  border-radius: 7px; font-size: 13px; color: rgba(255,255,255,.55);
  transition: all .15s ease; position: relative;
}
.sub-item:hover { color: #fff; background: rgba(255,255,255,.06); }
.sub-item.active {
  color: #fff; background: rgba(47,181,159,.16); font-weight: 600;
  animation: sub-breathe 2.4s ease-in-out infinite;
}
.sub-item.active::before {
  content: ''; position: absolute; left: -11px; top: 7px; bottom: 7px; width: 2px;
  border-radius: 2px; background: var(--primary);
}
@keyframes sub-breathe { 50% { box-shadow: 0 0 0 3px rgba(47,181,159,.07); } }
.sub-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub-empty { font-size: 12px; color: rgba(255,255,255,.35); padding: 4px 10px; }
.sub-item :deep(.status-dot) { flex-shrink: 0; }
.mini-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; background: #b9c6c2; }
.mini-dot.running { background: var(--ok); box-shadow: 0 0 0 3px rgba(47,181,159,.18); }
.mini-dot.creating { background: var(--warn); animation: dotpulse 1.2s ease-in-out infinite; }
.mini-dot.created { background: var(--primary); }
@keyframes dotpulse { 50% { opacity: .35; } }

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
  .nav-group { display: contents; }
  .sub-list, .node-list { display: none; }
  .side-foot { border: none; padding: 0; }
  .user-row { padding: 0; }
  .main { padding: 18px 16px; }
}
</style>
