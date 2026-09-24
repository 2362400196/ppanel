<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, errText } from '../api/client'
import { fmtBytes, fmtTime } from '../utils/format'
import { toastErr, toastOk } from './ui/toast'
import UIButton from './ui/UIButton.vue'
import UIModal from './ui/UIModal.vue'
import UIInput from './ui/UIInput.vue'
import UIProgress from './ui/UIProgress.vue'
import ConfirmDialog from './ui/ConfirmDialog.vue'
import CodeEditor from './CodeEditor.vue'

const props = defineProps({
  instanceId: { type: Number, required: true }
})

const path = ref('/')
const entries = ref([])
const loading = ref(false)
const uploads = ref([]) // [{name, percent}]
const fileInput = ref(null)

// 弹窗状态
const mkdirOpen = ref(false)
const mkdirName = ref('')
const renameOpen = ref(false)
const renameSrc = ref('')
const renameDst = ref('')
const delOpen = ref(false)
const delTarget = ref(null)
const editOpen = ref(false)
const editPath = ref('')
const editName = ref('')
const editContent = ref('')
const editSaving = ref(false)

const crumbs = computed(() => {
  const parts = path.value.split('/').filter(Boolean)
  return [{ name: '根目录', to: '/' }, ...parts.map((p, i) => ({
    name: p, to: '/' + parts.slice(0, i + 1).join('/')
  }))]
})

const join = (base, name) => (base === '/' ? '' : base) + '/' + name

async function load() {
  loading.value = true
  try {
    const { data } = await api.get(`/instances/${props.instanceId}/files`, { params: { path: path.value } })
    entries.value = data.entries
  } catch (e) {
    toastErr(errText(e))
  } finally {
    loading.value = false
  }
}

function openDir(row) {
  path.value = join(path.value, row.name)
  load()
}

function onCrumb(c) {
  path.value = c.to
  load()
}

async function uploadFiles(fileList) {
  for (const f of fileList) {
    const item = { name: f.name, percent: 0 }
    uploads.value.push(item)
    try {
      const fd = new FormData()
      fd.append('file', f)
      await api.put(`/instances/${props.instanceId}/files/upload`, fd, {
        params: { path: path.value },
        onUploadProgress: e => {
          if (e.total) item.percent = Math.round((e.loaded / e.total) * 100)
        }
      })
    } catch (e) {
      toastErr(`${f.name}：${errText(e)}`)
    }
  }
  uploads.value = []
  toastOk('上传完成')
  load()
}

async function download(row) {
  try {
    const res = await api.get(`/instances/${props.instanceId}/files/download`, {
      params: { path: join(path.value, row.name) },
      responseType: 'blob'
    })
    const url = URL.createObjectURL(res.data)
    const a = document.createElement('a')
    a.href = url
    a.download = row.name
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    toastErr(errText(e))
  }
}

async function openEditor(row) {
  try {
    const { data } = await api.get(`/instances/${props.instanceId}/files/content`, {
      params: { path: join(path.value, row.name) }
    })
    editPath.value = join(path.value, row.name)
    editName.value = row.name
    editContent.value = data.content
    editOpen.value = true
  } catch (e) {
    toastErr(errText(e))
  }
}

async function saveEdit() {
  editSaving.value = true
  try {
    await api.post(`/instances/${props.instanceId}/files/save`, {
      path: editPath.value, content: editContent.value
    })
    toastOk('已保存')
    editOpen.value = false
  } catch (e) {
    toastErr(errText(e))
  } finally {
    editSaving.value = false
  }
}

async function doMkdir() {
  if (!mkdirName.value.trim()) return
  try {
    await api.post(`/instances/${props.instanceId}/files/mkdir`, {
      path: join(path.value, mkdirName.value.trim())
    })
    toastOk('目录已创建')
    mkdirOpen.value = false
    mkdirName.value = ''
    load()
  } catch (e) {
    toastErr(errText(e))
  }
}

function askRename(row) {
  renameSrc.value = join(path.value, row.name)
  renameDst.value = row.name
  renameOpen.value = true
}

async function doRename() {
  try {
    await api.post(`/instances/${props.instanceId}/files/rename`, {
      src: renameSrc.value,
      dst: join(path.value, renameDst.value.trim())
    })
    toastOk('已重命名')
    renameOpen.value = false
    load()
  } catch (e) {
    toastErr(errText(e))
  }
}

function askDelete(row) {
  delTarget.value = row
  delOpen.value = true
}

async function doDelete() {
  try {
    await api.delete(`/instances/${props.instanceId}/files`, {
      params: { path: join(path.value, delTarget.value.name) }
    })
    toastOk('已删除')
    delOpen.value = false
    load()
  } catch (e) {
    toastErr(errText(e))
  }
}

onMounted(load)
</script>

<template>
  <div class="fm">
    <div class="fm-toolbar">
      <div class="crumbs">
        <template v-for="(c, i) in crumbs" :key="c.to">
          <span v-if="i" class="crumb-sep">/</span>
          <button class="crumb" :class="{ cur: i === crumbs.length - 1 }" @click="onCrumb(c)">
            {{ c.name }}
          </button>
        </template>
      </div>
      <div class="fm-actions">
        <UIButton type="ghost" @click="mkdirOpen = true">新建目录</UIButton>
        <UIButton type="ghost" @click="fileInput.click()">上传文件</UIButton>
        <UIButton type="ghost" @click="load">刷新</UIButton>
        <input
          ref="fileInput" type="file" multiple hidden
          @change="e => { uploadFiles(e.target.files); e.target.value = '' }"
        />
      </div>
    </div>

    <div v-if="uploads.length" class="upload-panel">
      <div v-for="u in uploads" :key="u.name" class="up-item">
        <span class="up-name mono">{{ u.name }}</span>
        <UIProgress :percent="u.percent" />
        <span class="up-pct">{{ u.percent }}%</span>
      </div>
    </div>

    <div class="fm-table card">
      <table>
        <thead>
          <tr><th>名称</th><th class="col-size">大小</th><th class="col-time">修改时间</th><th class="col-ops">操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="4" class="empty">加载中…</td></tr>
          <tr v-else-if="!entries.length"><td colspan="4" class="empty">空目录</td></tr>
          <tr v-for="row in entries" v-else :key="row.name" class="frow">
            <td>
              <button
                class="fname"
                :class="{ dir: row.is_dir }"
                @click="row.is_dir ? openDir(row) : null"
              >
                <i class="ficon" :class="row.is_dir ? 'dir-i' : 'file-i'" />
                <span class="mono">{{ row.name }}</span>
              </button>
            </td>
            <td class="col-size text-dim">{{ row.is_dir ? '-' : fmtBytes(row.size) }}</td>
            <td class="col-time text-dim">{{ fmtTime(row.mtime * 1000) }}</td>
            <td class="col-ops">
              <UIButton v-if="!row.is_dir" type="text" @click="openEditor(row)">编辑</UIButton>
              <UIButton v-if="!row.is_dir" type="text" @click="download(row)">下载</UIButton>
              <UIButton type="text" @click="askRename(row)">重命名</UIButton>
              <UIButton type="text" class="danger" @click="askDelete(row)">删除</UIButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

  <p class="fm-hint text-dim">文件存储在宿主机挂载目录，容器停止状态下同样可以上传与编辑。</p>

    <!-- 新建目录 -->
    <UIModal v-model:open="mkdirOpen" title="新建目录" width="360px">
      <UIInput v-model="mkdirName" placeholder="目录名称" @enter="doMkdir" />
      <template #footer>
        <UIButton type="ghost" @click="mkdirOpen = false">取消</UIButton>
        <UIButton @click="doMkdir">创建</UIButton>
      </template>
    </UIModal>

    <!-- 重命名 -->
    <UIModal v-model:open="renameOpen" title="重命名" width="360px">
      <UIInput v-model="renameDst" placeholder="新名称" @enter="doRename" />
      <template #footer>
        <UIButton type="ghost" @click="renameOpen = false">取消</UIButton>
        <UIButton @click="doRename">确认</UIButton>
      </template>
    </UIModal>

    <!-- 删除确认 -->
    <ConfirmDialog
      v-model:open="delOpen"
      title="确认删除"
      :message="`确定删除「${delTarget?.name}」吗？${delTarget?.is_dir ? '目录内所有内容都会被删除，' : ''}此操作不可恢复。`"
      confirm-text="删除"
      danger
      @confirm="doDelete"
    />

    <!-- 在线编辑 -->
    <UIModal v-model:open="editOpen" :title="`编辑 ${editName}`" width="min(900px, 92vw)" persistent>
      <div class="edit-box">
        <CodeEditor v-model="editContent" :filename="editName" />
      </div>
      <template #footer>
        <UIButton type="ghost" @click="editOpen = false">取消</UIButton>
        <UIButton :loading="editSaving" @click="saveEdit">保存</UIButton>
      </template>
    </UIModal>
  </div>
</template>

<style scoped>
.fm { display: flex; flex-direction: column; gap: 12px; }
.fm-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.crumbs { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; font-size: 13px; }
.crumb {
  border: none; background: transparent; cursor: pointer; font-family: inherit;
  color: var(--primary-strong); padding: 3px 6px; border-radius: 6px; font-size: 13px;
}
.crumb:hover { background: var(--primary-soft); }
.crumb.cur { color: var(--text); font-weight: 600; cursor: default; }
.crumb.cur:hover { background: transparent; }
.crumb-sep { color: var(--text-dim); }
.fm-actions { display: flex; gap: 8px; }

.upload-panel {
  display: flex; flex-direction: column; gap: 6px;
  background: var(--primary-soft-2); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: 10px 12px;
}
.up-item { display: grid; grid-template-columns: 200px 1fr 42px; align-items: center; gap: 12px; }
.up-name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.up-pct { font-size: 12px; color: var(--text-2); text-align: right; }

.fm-table { overflow: auto; max-height: 480px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; padding: 10px 14px; position: sticky; top: 0; z-index: 1;
  background: var(--primary-soft-2); color: var(--text-2); font-weight: 600;
  border-bottom: 1px solid var(--line); white-space: nowrap;
}
td { padding: 8px 14px; border-bottom: 1px solid var(--line); }
tbody tr:last-child td { border-bottom: none; }
.frow:hover { background: var(--primary-soft-2); }
.empty { text-align: center; color: var(--text-dim); padding: 32px 0; }
.col-size { width: 90px; }
.col-time { width: 160px; }
.col-ops { width: 220px; white-space: nowrap; }

.fname {
  display: inline-flex; align-items: center; gap: 8px;
  border: none; background: transparent; cursor: default;
  font-size: 13px; font-family: inherit; color: var(--text); padding: 3px 6px;
  border-radius: 6px;
}
.fname.dir { cursor: pointer; color: var(--primary-deep); font-weight: 600; }
.fname.dir:hover { background: var(--primary-soft); }
.ficon { width: 12px; height: 14px; flex-shrink: 0; border-radius: 2px; }
.dir-i {
  height: 11px; background: var(--primary); opacity: .85;
  border-radius: 2px 3px 2px 2px;
}
.file-i { border: 1.4px solid var(--text-dim); }

.fm-hint { font-size: 12px; }
.edit-box { height: 60vh; }
</style>
