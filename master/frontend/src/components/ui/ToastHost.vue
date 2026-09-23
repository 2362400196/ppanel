<script setup>
import { toasts } from './toast'
</script>

<template>
  <Teleport to="body">
    <div class="toast-host">
      <TransitionGroup name="toast">
        <div v-for="t in toasts" :key="t.id" class="toast" :class="`t-${t.type}`">
          <i class="mark" />
          <span>{{ t.message }}</span>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-host {
  position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
  z-index: 2000; display: flex; flex-direction: column; gap: 8px; align-items: center;
}
.toast {
  display: flex; align-items: center; gap: 8px;
  background: rgba(255,255,255,.95); backdrop-filter: blur(12px);
  border: 1px solid var(--line); border-radius: 10px;
  padding: 9px 16px; font-size: 13px; box-shadow: var(--shadow-lg);
}
.mark { width: 7px; height: 7px; border-radius: 50%; }
.t-ok .mark { background: var(--ok); }
.t-error .mark { background: var(--danger); }
.t-info .mark { background: var(--primary); }

.toast-enter-active, .toast-leave-active { transition: all .25s ease; }
.toast-enter-from { opacity: 0; transform: translateY(-10px); }
.toast-leave-to { opacity: 0; transform: translateY(-6px); }
</style>
