<script setup>
defineProps({
  tone: { type: String, default: 'primary' } // primary / ok / warn / danger / dim
})
</script>

<template>
  <span class="ui-tag" :class="`tag-${tone}`"><slot /></span>
</template>

<style scoped>
.ui-tag {
  display: inline-flex; align-items: center; height: 22px; padding: 0 9px;
  border-radius: 11px; font-size: 12px; font-weight: 500; white-space: nowrap;
}
.tag-primary { background: var(--primary-soft); color: var(--primary-deep); }
.tag-ok { background: var(--ok-soft); color: var(--primary-deep); }
.tag-warn { background: var(--warn-soft); color: #a9701c; }
.tag-danger { background: var(--danger-soft); color: var(--danger); }
.tag-dim { background: #eef3f2; color: var(--text-dim); }
</style>
