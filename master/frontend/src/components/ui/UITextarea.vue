<script setup>
defineProps({
  modelValue: String,
  placeholder: String,
  rows: { type: Number, default: 4 },
  mono: Boolean,
  disabled: Boolean
})
defineEmits(['update:modelValue'])
</script>

<template>
  <textarea
    class="ui-textarea"
    :class="{ mono }"
    :value="modelValue"
    :placeholder="placeholder"
    :rows="rows"
    :disabled="disabled"
    @input="$emit('update:modelValue', $event.target.value)"
  />
</template>

<style scoped>
.ui-textarea {
  width: 100%; padding: 10px 12px; resize: vertical;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--panel); color: var(--text); font-size: 13px; line-height: 1.6;
  font-family: inherit; outline: none;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.ui-textarea.mono { font-family: var(--font-mono); }
.ui-textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(47,181,159,.15); }
.ui-textarea:disabled { background: var(--bg); cursor: not-allowed; }
</style>
