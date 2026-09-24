<script setup>
import UIModal from './UIModal.vue'
import UIButton from './UIButton.vue'

defineProps({
  open: Boolean,
  title: { type: String, default: '确认操作' },
  message: String,
  confirmText: { type: String, default: '确认' },
  danger: Boolean,
  loading: Boolean
})
defineEmits(['update:open', 'confirm'])
</script>

<template>
  <UIModal :open="open" :title="title" width="380px" @update:open="$emit('update:open', $event)">
    <p class="msg"><slot>{{ message }}</slot></p>
    <template #footer>
      <UIButton type="ghost" @click="$emit('update:open', false)">取消</UIButton>
      <UIButton :type="danger ? 'danger' : 'primary'" :loading="loading" @click="$emit('confirm')">
        {{ confirmText }}
      </UIButton>
    </template>
  </UIModal>
</template>

<style scoped>
.msg { color: var(--text-2); line-height: 1.7; font-size: 13px; }
</style>
