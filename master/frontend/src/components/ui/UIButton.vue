<script setup>
defineProps({
  type: { type: String, default: 'primary' }, // primary / ghost / danger / text
  size: { type: String, default: 'sm' },       // sm / md
  loading: Boolean,
  disabled: Boolean,
  block: Boolean
})
</script>

<template>
  <button
    class="btn"
    :class="[`btn-${type}`, `btn-${size}`, { 'btn-block': block, 'is-loading': loading }]"
    :disabled="disabled || loading"
  >
    <span v-if="loading" class="btn-spin" />
    <span class="btn-label"><slot /></span>
  </button>
</template>

<style scoped>
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  border: 1px solid transparent; border-radius: var(--radius-sm);
  font-size: 13px; font-weight: 500; font-family: inherit;
  cursor: pointer; white-space: nowrap; user-select: none;
  transition: all .15s ease; background: transparent; color: var(--text);
}
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn-label { display: inline-flex; align-items: center; gap: 6px; }
.btn-sm { height: 30px; padding: 0 12px; }
.btn-md { height: 38px; padding: 0 18px; font-size: 14px; }
.btn-block { width: 100%; }

.btn-primary { background: var(--primary); color: #fff; box-shadow: 0 2px 8px rgba(47,181,159,.35); }
.btn-primary:hover:not(:disabled) { background: var(--primary-strong); transform: translateY(-1px); }
.btn-primary:active:not(:disabled) { transform: none; }

.btn-ghost { border-color: var(--line); background: var(--panel); color: var(--text-2); }
.btn-ghost:hover:not(:disabled) { border-color: var(--primary); color: var(--primary-strong); }

.btn-danger { background: var(--danger); color: #fff; }
.btn-danger:hover:not(:disabled) { filter: brightness(1.08); }
.btn-danger.btn-ghost-style { background: transparent; }

.btn-text { color: var(--primary-strong); padding: 0 6px; }
.btn-text:hover:not(:disabled) { opacity: .8; }
.btn-text.danger { color: var(--danger); }

.btn-spin {
  width: 12px; height: 12px; border-radius: 50%;
  border: 2px solid rgba(255,255,255,.35); border-top-color: #fff;
  animation: spin .7s linear infinite;
}
.btn-ghost .btn-spin, .btn-text .btn-spin {
  border-color: rgba(47,181,159,.3); border-top-color: var(--primary);
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
