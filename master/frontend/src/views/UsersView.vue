<script setup>
import { onMounted, ref, watch } from 'vue'
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
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const userCols = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'username', label: '用户名' },
  { key: 'role', label: '角色', width: '100px' },
  { key: 'balance', label: '钱包余额', width: '110px' },
  { key: 'created_at', label: '创建时间', width: '170px' },
  { key: 'ops', label: '', width: '90px' }
]
const createUserOpen = ref(false)
const newUser = ref({ username: '', password: '', role: 'user' })
const creatingUser = ref(false)

async function loadUsers() {
  try {
    const { data } = await api.get('/admin/users', {
      params: { page: page.value, page_size: pageSize.value, keyword: keyword.value.trim() }
    })
    users.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    toastErr(errText(e))
  }
}

// 用户名搜索（后端下推），300ms 防抖
let kwTimer
watch(keyword, () => {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(() => {
    page.value = 1
    loadUsers()
  }, 300)
})

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

// ---------- 余额调整（加/减） ----------
const adjOpen = ref(false)
const adjUser = ref(null)
const adjMode = ref('add')           // add | sub
const adjAmount = ref('')
const adjNote = ref('')
const adjusting = ref(false)

function askAdjust(u) {
  adjUser.value = u
  adjMode.value = 'add'
  adjAmount.value = ''
  adjNote.value = ''
  adjOpen.value = true
}

async function doAdjust() {
  const yuan = parseFloat(adjAmount.value)
  if (!(yuan > 0)) { toastErr('请输入正确的金额'); return }
  adjusting.value = true
  try {
    const cents = Math.round(yuan * 100) * (adjMode.value === 'add' ? 1 : -1)
    const { data } = await api.post('/admin/pay/adjust', {
      user_id: adjUser.value.id, amount_cents: cents, note: adjNote.value.trim()
    })
    toastOk(data.detail || '已调整')
    adjOpen.value = false
    loadUsers()
  } catch (e) {
    toastErr(errText(e))
  } finally {
    adjusting.value = false
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
      <span class="text-dim">共 {{ total }} 个用户</span>
      <div class="sec-right">
        <UIInput v-model="keyword" style="width:220px" placeholder="搜索用户名" />
        <UIButton @click="createUserOpen = true">新建用户</UIButton>
      </div>
    </div>

    <UITable :columns="userCols" :rows="users"
             :total="total" v-model:page="page" v-model:pageSize="pageSize"
             @change="loadUsers">
      <template #col-role="{ row }">
        <UITag :tone="row.role === 'admin' ? 'warn' : 'primary'">{{ row.role === 'admin' ? '管理员' : '用户' }}</UITag>
      </template>
      <template #col-balance="{ row }">
        <span class="balance">¥{{ ((row.balance_cents || 0) / 100).toFixed(2) }}</span>
      </template>
      <template #col-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
      <template #col-ops="{ row }">
        <UIButton type="text" @click="askAdjust(row)">余额</UIButton>
      </template>
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

    <!-- 余额调整（加/减） -->
    <UIModal v-model:open="adjOpen" :title="`余额调整 · ${adjUser?.username || ''}`" width="380px">
      <div class="form">
        <div class="mode-row">
          <button class="mode-btn" :class="{ active: adjMode === 'add' }" @click="adjMode = 'add'">加余额</button>
          <button class="mode-btn sub" :class="{ active: adjMode === 'sub' }" @click="adjMode = 'sub'">减余额</button>
        </div>
        <label class="field">
          <span>金额（元）</span>
          <UIInput v-model="adjAmount" type="number" min="0" step="0.01" placeholder="如 10 或 0.01" />
        </label>
        <label class="field">
          <span>备注（进流水，可不填）</span>
          <UIInput v-model="adjNote" placeholder="如：活动赠送 / 退款扣回" />
        </label>
        <p class="adj-tip text-dim">当前余额 ¥{{ ((adjUser?.balance_cents || 0) / 100).toFixed(2) }}，调整记录可在「订单支付」流水查看。</p>
      </div>
      <template #footer>
        <UIButton type="ghost" @click="adjOpen = false">取消</UIButton>
        <UIButton :loading="adjusting" @click="doAdjust">{{ adjMode === 'add' ? '确认加款' : '确认扣减' }}</UIButton>
      </template>
    </UIModal>
  </div>
</template>

<style scoped>
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.sec-right { display: flex; align-items: center; gap: 10px; }
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field span { font-size: 12px; font-weight: 600; color: var(--text-2); }
.balance { font-weight: 600; font-variant-numeric: tabular-nums; }
.mode-row { display: flex; gap: 8px; }
.mode-btn {
  flex: 1; padding: 9px 0; border: 1px solid var(--line); border-radius: 10px;
  background: var(--panel); font-size: 13px; cursor: pointer; color: var(--text-2);
  font-family: inherit; transition: all .15s;
}
.mode-btn:hover { border-color: var(--primary); }
.mode-btn.active { border-color: var(--primary); background: var(--primary-soft); color: var(--primary-strong); font-weight: 700; }
.mode-btn.sub.active { border-color: var(--danger, #e5484d); background: color-mix(in srgb, var(--danger, #e5484d) 8%, transparent); color: var(--danger, #e5484d); }
.adj-tip { font-size: 12px; margin: 0; }
</style>
