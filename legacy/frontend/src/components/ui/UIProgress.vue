<script setup>
defineProps({
  percent: { type: Number, default: 0 },
  tone: { type: String, default: 'primary' } // primary / danger
})
</script>

<template>
  <div class="ui-progress">
    <div class="bar" :class="`bar-${tone}`" :style="{ width: Math.min(100, Math.max(0, percent)) + '%' }" />
  </div>
</template>

<style scoped>
.ui-progress {
  height: 6px; border-radius: 3px; background: #e8efed; overflow: hidden;
}
.bar {
  height: 100%; border-radius: 3px;
  transition: width .3s ease;
}
.bar-primary { background: linear-gradient(90deg, var(--primary), var(--primary-strong)); }
.bar-danger { background: var(--danger); }
</style>
