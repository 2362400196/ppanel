<script setup>
defineProps({
  modelValue: [String, Number],
  type: { type: String, default: 'text' },
  placeholder: String,
  disabled: Boolean
})
defineEmits(['update:modelValue', 'enter'])
</script>

<template>
  <input
    class="ui-input"
    :type="type"
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    @input="$emit('update:modelValue', $event.target.value)"
    @keydown.enter="$emit('enter', $event)"
  />
</template>

<style scoped>
.ui-input {
  width: 100%; height: 36px; padding: 0 12px;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--panel); color: var(--text); font-size: 13px; font-family: inherit;
  outline: none; transition: border-color .15s ease, box-shadow .15s ease;
}
.ui-input::placeholder { color: var(--text-dim); }
.ui-input:hover:not(:disabled) { border-color: var(--primary); }
.ui-input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(47,181,159,.15); }
.ui-input:disabled { background: var(--bg); cursor: not-allowed; }
</style>
