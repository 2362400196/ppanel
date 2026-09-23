<script setup>
defineProps({
  modelValue: [String, Number],
  options: { type: Array, default: () => [] }, // [{value, label}] 或字符串数组
  disabled: Boolean
})
defineEmits(['update:modelValue'])

const norm = o => (typeof o === 'object' ? o : { value: o, label: o })
</script>

<template>
  <div class="ui-select-wrap">
    <select
      class="ui-select"
      :value="modelValue"
      :disabled="disabled"
      @change="$emit('update:modelValue', $event.target.value)"
    >
      <option v-for="o in options.map(norm)" :key="o.value" :value="o.value">{{ o.label }}</option>
    </select>
    <span class="arrow" aria-hidden="true" />
  </div>
</template>

<style scoped>
.ui-select-wrap { position: relative; width: 100%; }
.ui-select {
  width: 100%; height: 36px; padding: 0 30px 0 12px; appearance: none;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--panel); color: var(--text); font-size: 13px; font-family: inherit;
  outline: none; cursor: pointer;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.ui-select:hover:not(:disabled), .ui-select:focus { border-color: var(--primary); }
.ui-select:focus { box-shadow: 0 0 0 3px rgba(47,181,159,.15); }
.ui-select:disabled { background: var(--bg); cursor: not-allowed; }
.arrow {
  position: absolute; right: 12px; top: 50%; pointer-events: none;
  width: 7px; height: 7px; border-right: 1.5px solid var(--text-dim);
  border-bottom: 1.5px solid var(--text-dim);
  transform: translateY(-70%) rotate(45deg);
}
</style>
