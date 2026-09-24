<script setup>
defineProps({
  tabs: { type: Array, required: true }, // [{key, label}]
  modelValue: String
})
defineEmits(['update:modelValue'])
</script>

<template>
  <div class="ui-tabs">
    <button
      v-for="t in tabs"
      :key="t.key"
      class="tab"
      :class="{ active: modelValue === t.key }"
      @click="$emit('update:modelValue', t.key)"
    >
      {{ t.label }}
    </button>
  </div>
</template>

<style scoped>
.ui-tabs {
  display: inline-flex; gap: 4px; padding: 4px;
  background: #eef3f2; border-radius: 10px;
}
.tab {
  position: relative; border: none; background: transparent; cursor: pointer;
  height: 32px; padding: 0 16px; border-radius: 7px;
  font-size: 13px; font-weight: 500; color: var(--text-2); font-family: inherit;
  transition: all .15s ease;
}
.tab:hover { color: var(--primary-strong); }
.tab.active {
  background: var(--panel); color: var(--primary-strong); font-weight: 600;
  box-shadow: 0 2px 8px rgba(28,54,48,.1);
}
</style>
