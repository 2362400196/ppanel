<script setup>
defineProps({
  columns: { type: Array, required: true }, // [{key, label, width?}]
  rows: { type: Array, default: () => [] },
  loading: Boolean
})
</script>

<template>
  <div class="ui-table-wrap">
    <table class="ui-table">
      <thead>
        <tr>
          <th v-for="c in columns" :key="c.key" :style="c.width ? { width: c.width } : null">
            {{ c.label }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading">
          <td :colspan="columns.length" class="empty">加载中…</td>
        </tr>
        <tr v-else-if="!rows.length">
          <td :colspan="columns.length" class="empty">暂无数据</td>
        </tr>
        <tr v-for="(row, i) in rows" v-else :key="i" class="row">
          <td v-for="c in columns" :key="c.key">
            <slot :name="'col-' + c.key" :row="row">{{ row[c.key] }}</slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.ui-table-wrap {
  border: 1px solid var(--line); border-radius: var(--radius);
  overflow: auto; background: var(--panel);
}
.ui-table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; padding: 10px 14px; font-weight: 600; color: var(--text-2);
  background: var(--primary-soft-2); border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
td { padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
.row { transition: background .12s ease; }
.row:hover { background: var(--primary-soft-2); }
.empty { text-align: center; color: var(--text-dim); padding: 32px 0; }
</style>
