<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UIInput from '../components/ui/UIInput.vue'

const router = useRouter()
const auth = useAuthStore()

const oldPwd = ref('')
const newPwd = ref('')
const newPwd2 = ref('')
const saving = ref(false)

async function changePwd() {
  if (!oldPwd.value || !newPwd.value) return toastErr('请填写完整')
  if (newPwd.value.length < 6) return toastErr('新密码至少 6 位')
  if (newPwd.value !== newPwd2.value) return toastErr('两次输入的新密码不一致')
  saving.value = true
  try {
    await api.post('/auth/change-password', { old_password: oldPwd.value, new_password: newPwd.value })
    toastOk('密码已修改')
    oldPwd.value = newPwd.value = newPwd2.value = ''
  } catch (e) {
    toastErr(errText(e))
  } finally {
    saving.value = false
  }
}

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <div class="settings">
    <div class="card block">
      <h2 class="block-title">账号</h2>
      <div class="row">
        <span class="k">用户名</span>
        <span class="v">{{ auth.user?.username }}</span>
      </div>
      <div class="row">
        <span class="k">角色</span>
        <span class="v">{{ auth.isAdmin ? '管理员' : '用户' }}</span>
      </div>
      <div class="row">
        <span class="k">退出登录</span>
        <UIButton type="text" class="danger" @click="logout">退出</UIButton>
      </div>
    </div>

    <div class="card block">
      <h2 class="block-title">修改密码</h2>
      <div class="form">
        <label class="f-item">
          <span class="k">原密码</span>
          <UIInput v-model="oldPwd" type="password" placeholder="请输入原密码" />
        </label>
        <label class="f-item">
          <span class="k">新密码</span>
          <UIInput v-model="newPwd" type="password" placeholder="至少 6 位" />
        </label>
        <label class="f-item">
          <span class="k">确认新密码</span>
          <UIInput v-model="newPwd2" type="password" placeholder="再次输入新密码" />
        </label>
        <UIButton :loading="saving" @click="changePwd">保存修改</UIButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings { max-width: 560px; display: flex; flex-direction: column; gap: 18px; }
.block { padding: 20px; }
.block-title { font-size: 14px; font-weight: 700; margin: 0 0 14px; }
.row { display: flex; align-items: center; justify-content: space-between; padding: 9px 0; font-size: 14px; border-bottom: 1px dashed var(--line); }
.row:last-child { border-bottom: none; }
.k { color: var(--text-dim); }
.v { font-weight: 600; }
.form { display: flex; flex-direction: column; gap: 12px; }
.f-item { display: flex; flex-direction: column; gap: 6px; }
.f-item .k { font-size: 13px; }
</style>
