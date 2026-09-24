<script setup>
import { onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { fmtTime } from '../utils/format'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITable from '../components/ui/UITable.vue'
import UITag from '../components/ui/UITag.vue'
import UIModal from '../components/ui/UIModal.vue'
import UIInput from '../components/ui/UIInput.vue'
import UISelect from '../components/ui/UISelect.vue'

const users = ref([])
const userCols = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'username', label: '用户名' },
  { key: 'role', label: '角色', width: '100px' },
  { key: 'created_at', label: '创建时间', width: '180px' }
]
const createUserOpen = ref(false)
const newUser = ref({ username: '', password: '', role: 'user' })
const creatingUser = ref(false)

async function loadUsers() {
  try {
    const { data } = await api.get('/admin/users')
    users.value = data
  } catch (e) {
    toastErr(errText(e))
  }
}

async function doCreateUser() {
  creatingUser.value = true
  try {
    await api.post('/admin/users', {
      username: newUser.value.username.trim(),
      password: newUser.value.password,
      role: newUser.value.role
    })
    toastOk('用户已创建')
    createUserOpen.value = false
    newUser.value = { username: '', password: '', role: 'user' }
    loadUsers()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    creatingUser.value = false
  }
}

onMounted(loadUsers)
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">用户管理</h1>
        <p class="page-sub">平台账号与角色管理</p>
      </div>
    </div>

    <div class="sec-head">
      <span class="text-dim">共 {{ users.length }} 个用户</span>
      <UIButton @click="createUserOpen = true">新建用户</UIButton>
    </div>

    <UITable :columns="userCols" :rows="users">
      <template #col-role="{ row }">
        <UITag :tone="row.role === 'admin' ? 'warn' : 'primary'">{{ row.role === 'admin' ? '管理员' : '用户' }}</UITag>
      </template>
      <template #col-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
    </UITable>

    <!-- 新建用户 -->
    <UIModal v-model:open="createUserOpen" title="新建用户" width="380px">
      <div class="form">
        <label class="field">
          <span>用户名</span>
          <UIInput v-model="newUser.username" placeholder="2-64 个字符" />
        </label>
        <label class="field">
          <span>密码</span>
          <UIInput v-model="newUser.password" type="password" placeholder="至少 6 位" />
        </label>
        <label class="field">
          <span>角色</span>
          <UISelect v-model="newUser.role" :options="[{ value: 'user', label: '普通用户' }, { value: 'admin', label: '管理员' }]" />
        </label>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="createUserOpen = false">取消</UIButton>
        <UIButton :loading="creatingUser" @click="doCreateUser">创建</UIButton>
      </template>
    </UIModal>
  </div>
</template>

<style scoped>
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
</style>
