<script setup>
defineProps({ text: { type: String, default: '暂无内容' } })
</script>

<template>
  <div class="empty-state">
    <i class="glyph" />
    <p>{{ text }}</p>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 12px; padding: 48px 0; color: var(--text-dim); font-size: 13px;
}
.glyph {
  width: 44px; height: 44px; border-radius: 12px;
  background: var(--primary-soft-2); border: 1px dashed var(--line);
  position: relative;
}
.glyph::after {
  content: ''; position: absolute; inset: 14px;
  border: 1.5px solid var(--line); border-radius: 4px;
}
</style>
