<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { errText } from '../api/client'
import { toastErr } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'

const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!username.value || !password.value) return toastErr('请输入用户名和密码')
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    router.push('/instances')
  } catch (e) {
    toastErr(errText(e))
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
      <form @submit.prevent="submit">
        <label class="field">
          <span>用户名</span>
          <UIInput v-model="username" placeholder="请输入用户名" />
        </label>
        <label class="field">
          <span>密码</span>
          <UIInput v-model="password" type="password" placeholder="请输入密码" />
        </label>
        <UIButton type="primary" size="md" block :loading="loading">登 录</UIButton>
      </form>
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

.brand { display: flex; align-items: center; gap: 14px; margin-bottom: 28px; }
.logo {
  width: 46px; height: 46px; border-radius: 14px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--primary), var(--primary-deep));
  color: #fff; font-weight: 800; font-size: 23px; font-family: var(--font-mono);
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 6px 18px rgba(47,181,159,.45);
}
.brand h1 { font-size: 20px; letter-spacing: 1px; }
.brand p { font-size: 12px; color: var(--text-dim); margin-top: 2px; }

form { display: flex; flex-direction: column; gap: 16px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
form .btn-md { margin-top: 6px; }
</style>
