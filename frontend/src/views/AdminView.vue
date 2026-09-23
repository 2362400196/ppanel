<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errText } from '../api/client'
import { fmtTime } from '../utils/format'
import { toastErr, toastOk } from '../components/ui/toast'
import UIButton from '../components/ui/UIButton.vue'
import UITabs from '../components/ui/UITabs.vue'
import UITable from '../components/ui/UITable.vue'
import UITag from '../components/ui/UITag.vue'
import UIModal from '../components/ui/UIModal.vue'
import UIInput from '../components/ui/UIInput.vue'
import UISelect from '../components/ui/UISelect.vue'
import StatusDot from '../components/ui/StatusDot.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'

const router = useRouter()
const tab = ref('users')

// 用户管理
const users = ref([])
const userCols = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'username', label: '用户名' },
  { key: 'role', label: '角色', width: '100px' },
  { key: 'created_at', label: '创建时间', width: '180px' },
  { key: 'ops', label: '', width: '90px' }
]
const createUserOpen = ref(false)
const newUser = ref({ username: '', password: '', role: 'user' })
const creatingUser = ref(false)

// 实例管理
const instances = ref([])
const instCols = [
  { key: 'id', label: 'ID', width: '56px' },
  { key: 'name', label: '名称' },
  { key: 'owner', label: '归属用户', width: '110px' },
  { key: 'image', label: '镜像' },
  { key: 'ext_port', label: '外部端口', width: '90px' },
  { key: 'status', label: '状态', width: '110px' },
  { key: 'created_at', label: '创建时间', width: '170px' },
  { key: 'ops', label: '', width: '130px' }
]
const delOpen = ref(false)
const delTarget = ref(null)

const ownerMap = computed(() => Object.fromEntries(users.value.map(u => [u.id, u.username])))

async function loadUsers() {
  try {
    const { data } = await api.get('/admin/users')
    users.value = data
  } catch (e) {
    toastErr(errText(e))
  }
}

async function loadInstances() {
  try {
    const { data } = await api.get('/instances', { params: { all: 1 } })
    instances.value = data
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

async function doDeleteInstance() {
  try {
    const { data } = await api.delete(`/instances/${delTarget.value.id}`)
    toastOk(data.detail || '已删除')
    delOpen.value = false
    loadInstances()
  } catch (e) {
    toastErr(errText(e))
  }
}

async function power(inst) {
  const action = inst.status === 'running' ? 'stop' : 'start'
  try {
    await api.post(`/instances/${inst.id}/${action}`)
    toastOk(action === 'start' ? '已启动' : '已停止')
    loadInstances()
  } catch (e) {
    toastErr(errText(e))
  }
}

let timer
onMounted(() => {
  loadUsers()
  loadInstances()
  timer = setInterval(loadInstances, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">管理后台</h1>
        <p class="page-sub">用户与全部实例的统一管理</p>
      </div>
    </div>

    <UITabs v-model="tab" :tabs="[{ key: 'users', label: '用户管理' }, { key: 'instances', label: '实例管理' }]" class="tabs" />

    <template v-if="tab === 'users'">
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
    </template>

    <template v-else>
      <div class="sec-head">
        <span class="text-dim">共 {{ instances.length }} 个实例</span>
        <UIButton type="ghost" @click="loadInstances">刷新</UIButton>
      </div>
      <UITable :columns="instCols" :rows="instances">
        <template #col-name="{ row }">
          <button class="link mono" @click="router.push(`/instances/${row.id}`)">{{ row.name }}</button>
        </template>
        <template #col-owner="{ row }">{{ ownerMap[row.user_id] || `user#${row.user_id}` }}</template>
        <template #col-status="{ row }">
          <StatusDot :status="row.status" />
        </template>
        <template #col-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
        <template #col-ops="{ row }">
          <UIButton v-if="row.status === 'running'" type="text" @click="power(row)">停止</UIButton>
          <UIButton v-else type="text" :disabled="row.status === 'creating'" @click="power(row)">启动</UIButton>
          <UIButton type="text" class="danger" @click="delTarget = row; delOpen = true">删除</UIButton>
        </template>
      </UITable>
    </template>

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

    <ConfirmDialog
      v-model:open="delOpen"
      title="删除实例"
      :message="`确定删除用户「${ownerMap[delTarget?.user_id]}」的实例「${delTarget?.name}」吗？容器将被删除，宿主机目录保留。`"
      confirm-text="删除"
      danger
      @confirm="doDeleteInstance"
    />
  </div>
</template>

<style scoped>
.tabs { margin-bottom: 18px; }
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.link { border: none; background: transparent; cursor: pointer; color: var(--primary-strong); font-size: 13px; font-family: inherit; font-weight: 600; }
.link:hover { text-decoration: underline; }
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
</style>
