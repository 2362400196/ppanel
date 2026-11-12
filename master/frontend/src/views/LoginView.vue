<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { api, errText } from '../api/client'
import { toastErr } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'

const router = useRouter()
const auth = useAuthStore()

const mode = ref('login')          // login | register
const username = ref('')
const password = ref('')
const password2 = ref('')
const inviteCode = ref('')
const loading = ref(false)

// 注册开关由后端控制（ALLOW_REGISTER / REGISTER_INVITE_CODE）
const regEnabled = ref(false)
const needInvite = ref(false)

onMounted(async () => {
  try {
    const { data } = await api.get('/auth/register-config')
    regEnabled.value = !!data.enabled
    needInvite.value = !!data.need_invite
  } catch { regEnabled.value = false }
})

function switchMode(m) {
  if (loading.value) return
  mode.value = m
  password.value = ''
  password2.value = ''
}

async function submit() {
  if (loading.value) return
  if (!username.value || !password.value) return toastErr('请输入用户名和密码')
  loading.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(username.value, password.value)
    } else {
      const name = username.value.trim()
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/.test(name))
        throw new Error('用户名需 3-32 位，仅支持字母、数字、下划线、中划线和点，且以字母或数字开头')
      if (password.value.length < 8) throw new Error('密码至少 8 位')
      if (password.value !== password2.value) throw new Error('两次输入的密码不一致')
      if (needInvite.value && !inviteCode.value.trim()) throw new Error('请填写邀请码')
      await auth.register(name, password.value, inviteCode.value.trim())
    }
    router.push('/instances')
  } catch (e) {
    toastErr(e?.message && !e?.response ? e.message : errText(e))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="blob b1" />
    <div class="blob b2" />
    <div class="login-card">
      <div class="brand">
        <span class="logo">P</span>
        <div>
          <h1>PPanel</h1>
          <p>Python 环境容器托管面板</p>
        </div>
      </div>

      <div v-if="regEnabled" class="tabs">
        <button class="tab" :class="{ on: mode === 'login' }" @click="switchMode('login')">登录</button>
        <button class="tab" :class="{ on: mode === 'register' }" @click="switchMode('register')">注册</button>
      </div>

      <form @submit.prevent="submit">
        <label class="field">
          <span>用户名</span>
          <UIInput v-model="username" placeholder="请输入用户名" />
        </label>
        <label class="field">
          <span>密码</span>
          <UIInput v-model="password" type="password"
                   :placeholder="mode === 'register' ? '至少 8 位' : '请输入密码'" />
        </label>
        <template v-if="mode === 'register'">
          <label class="field">
            <span>确认密码</span>
            <UIInput v-model="password2" type="password" placeholder="请再次输入密码" />
          </label>
          <label v-if="needInvite" class="field">
            <span>邀请码</span>
            <UIInput v-model="inviteCode" placeholder="请填写管理员提供的邀请码" />
          </label>
        </template>

        <UIButton type="primary" size="md" block :loading="loading">
          {{ mode === 'login' ? '登 录' : '注 册并登录' }}
        </UIButton>
      </form>

      <p v-if="regEnabled" class="switch-tip">
        <template v-if="mode === 'login'">
          还没有账号？<a @click.prevent="switchMode('register')">立即注册</a>
        </template>
        <template v-else>
          已有账号？<a @click.prevent="switchMode('login')">去登录</a>
        </template>
      </p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(160deg, #f2f9f7 0%, #e7f3f0 55%, #dcefea 100%);
  position: relative; overflow: hidden;
}
.blob { position: absolute; border-radius: 50%; filter: blur(70px); opacity: .5; }
.b1 { width: 420px; height: 420px; background: rgba(47,181,159,.35); top: -120px; right: -80px; }
.b2 { width: 360px; height: 360px; background: rgba(23,105,92,.22); bottom: -100px; left: -60px; }

.login-card {
  position: relative; z-index: 1; width: 380px;
  background: rgba(255,255,255,.85); backdrop-filter: blur(20px) saturate(1.4);
  border: 1px solid rgba(255,255,255,.7); border-radius: 20px;
  box-shadow: var(--shadow-lg); padding: 36px 34px 32px;
  animation: rise .5s ease;
}
@keyframes rise { from { opacity: 0; transform: translateY(16px); } }

.brand { display: flex; align-items: center; gap: 14px; margin-bottom: 22px; }
.logo {
  width: 46px; height: 46px; border-radius: 14px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--primary), var(--primary-deep));
  color: #fff; font-weight: 800; font-size: 23px; font-family: var(--font-mono);
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 6px 18px rgba(47,181,159,.45);
}
.brand h1 { font-size: 20px; letter-spacing: 1px; }
.brand p { font-size: 12px; color: var(--text-dim); margin-top: 2px; }

.tabs {
  display: flex; gap: 4px; padding: 3px; margin-bottom: 18px;
  background: color-mix(in srgb, var(--primary) 8%, transparent); border-radius: 10px;
}
.tab {
  flex: 1; padding: 7px 0; border: none; border-radius: 8px; background: transparent;
  font-family: inherit; font-size: 13px; color: var(--text-2); cursor: pointer;
  transition: background .18s, color .18s, box-shadow .18s;
}
.tab.on {
  background: #fff; color: var(--primary); font-weight: 600;
  box-shadow: 0 1px 4px rgba(23,105,92,.12);
}

form { display: flex; flex-direction: column; gap: 16px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
form .btn-md { margin-top: 6px; }

.switch-tip { margin-top: 16px; text-align: center; font-size: 12px; color: var(--text-dim); }
.switch-tip a { color: var(--primary); cursor: pointer; font-weight: 600; }
.switch-tip a:hover { text-decoration: underline; }
</style>
