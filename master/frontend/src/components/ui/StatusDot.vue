<script setup>
import { STATUS_TEXT, STATUS_TONE } from '../../utils/format'
defineProps({ status: String })
</script>

<template>
  <span class="status" :class="`s-${status}`">
    <i class="dot" />
    <span>{{ STATUS_TEXT[status] || status }}</span>
  </span>
</template>

<style scoped>
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; }
.s-running .dot { background: var(--ok); box-shadow: 0 0 0 3px rgba(47,181,159,.18); }
.s-running span { color: var(--primary-deep); font-weight: 600; }
.s-exited .dot { background: #b9c6c2; }
.s-exited span { color: var(--text-dim); }
.s-creating .dot { background: var(--warn); animation: pulse 1.2s ease-in-out infinite; }
.s-creating span { color: #a9701c; }
.s-created .dot { background: var(--primary); }
@keyframes pulse { 50% { opacity: .35; } }
</style>
