import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true, bare: true }
  },
  {
    path: '/',
    name: 'instances',
    component: () => import('../views/InstancesView.vue')
  },
  {
    path: '/instances/:id',
    name: 'instance-detail',
    component: () => import('../views/InstanceDetailView.vue')
  },
  {
    path: '/shop',
    name: 'shop',
    component: () => import('../views/ShopView.vue')
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('../views/SettingsView.vue')
  },
  {
      path: '/admin',
      name: 'admin',
      component: () => import('../views/PlansView.vue'),
      meta: { admin: true },
    },
    {
      path: '/users',
      name: 'users',
      component: () => import('../views/UsersView.vue'),
      meta: { admin: true },
    },
    {
      path: '/manage',
      name: 'manage',
      component: () => import('../views/AdminInstancesView.vue'),
      meta: { admin: true },
    },
  {
    path: '/docker',
    name: 'docker',
    component: () => import('../views/DockerView.vue'),
    meta: { admin: true }
  },
  {
    path: '/nodes',
    name: 'nodes',
    component: () => import('../views/NodesView.vue'),
    meta: { admin: true }
  },
  { path: '/:pathMatch(.*)*', redirect: '/' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(async to => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  if (!auth.token) return '/login'
  if (!auth.user) {
    try {
      await auth.fetchMe()
    } catch {
      return '/login'
    }
  }
  if (to.meta.admin && !auth.isAdmin) return '/'
  // 管理员视角专注管理：用户功能页（仪表盘/商城）重定向到管理后台
  if (auth.isAdmin && ['instances', 'shop'].includes(to.name)) return '/admin'
  return true
})

export default router
