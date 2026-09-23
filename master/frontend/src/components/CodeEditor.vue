<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import tsWorker from 'monaco-editor/language/typescript/ts.worker.js?worker'
import jsonWorker from 'monaco-editor/language/json/json.worker.js?worker'
import htmlWorker from 'monaco-editor/language/html/html.worker.js?worker'
import cssWorker from 'monaco-editor/language/css/css.worker.js?worker'

// VS Code 内核（Monaco）worker 配置
self.MonacoEnvironment = {
  getWorker(_id, label) {
    if (label === 'json') return new jsonWorker()
    if (label === 'css' || label === 'scss' || label === 'less') return new cssWorker()
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new htmlWorker()
    if (label === 'typescript' || label === 'javascript') return new tsWorker()
    return new editorWorker()
  }
}

monaco.editor.defineTheme('ppanel-light', {
  base: 'vs',
  inherit: true,
  rules: [],
  colors: {
    'editor.background': '#fbfdfc',
    'editorLineNumber.foreground': '#9fb3ad',
    'editorLineNumber.activeForeground': '#2fb59f',
    'editor.selectionBackground': '#bfe8df',
    'editor.lineHighlightBackground': '#eef7f4'
  }
})
monaco.editor.defineTheme('ppanel-dark', {
  base: 'vs-dark',
  inherit: true,
  rules: [],
  colors: {
    'editor.background': '#0c1613',
    'editorLineNumber.foreground': '#3c544d',
    'editorLineNumber.activeForeground': '#2fb59f',
    'editor.selectionBackground': '#1d4a40',
    'editor.lineHighlightBackground': '#11201b'
  }
})

const props = defineProps({
  modelValue: String,
  filename: String,
  readonly: Boolean
})
const emit = defineEmits(['update:modelValue'])

const dark = ref(true)
const box = ref(null)
let editor = null

const EXT_LANG = {
  py: 'python', pyw: 'python',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', json: 'json', html: 'html', htm: 'html',
  css: 'css', scss: 'scss', less: 'less', md: 'markdown',
  sh: 'shell', bash: 'shell', yml: 'yaml', yaml: 'yaml',
  sql: 'sql', xml: 'xml', ini: 'ini', toml: 'ini', conf: 'ini',
  go: 'go', rs: 'rust', java: 'java', c: 'c', cpp: 'cpp', h: 'cpp'
}

function detectLang(name) {
  const ext = (name || '').split('.').pop().toLowerCase()
  return EXT_LANG[ext] || 'plaintext'
}

onMounted(() => {
  editor = monaco.editor.create(box.value, {
    value: props.modelValue || '',
    language: detectLang(props.filename),
    theme: dark.value ? 'ppanel-dark' : 'ppanel-light',
    automaticLayout: true,
    fontSize: 13,
    minimap: { enabled: false },
    tabSize: 4,
    scrollBeyondLastLine: false,
    readOnly: !!props.readonly,
    padding: { top: 10, bottom: 10 },
    renderLineHighlight: 'all',
    smoothScrolling: true,
    cursorBlinking: 'smooth',
    fontFamily: "'JetBrains Mono', 'Cascadia Code', Consolas, 'Courier New', monospace"
  })
  editor.onDidChangeModelContent(() => {
    emit('update:modelValue', editor.getValue())
  })
})

watch(() => props.modelValue, v => {
  if (editor && v != null && editor.getValue() !== v) {
    editor.setValue(v)
  }
})

watch(() => props.filename, name => {
  if (editor) {
    monaco.editor.setModelLanguage(editor.getModel(), detectLang(name))
  }
})

watch(dark, d => {
  monaco.editor.setTheme(d ? 'ppanel-dark' : 'ppanel-light')
})

onBeforeUnmount(() => editor?.dispose())
</script>

<template>
  <div class="editor" :class="{ dark }">
    <div class="editor-head">
      <span class="fname mono">{{ filename }}</span>
      <div class="theme-switch">
        <button :class="{ on: !dark }" @click="dark = false">亮</button>
        <button :class="{ on: dark }" @click="dark = true">暗</button>
      </div>
    </div>
    <div ref="box" class="editor-box" />
  </div>
</template>

<style scoped>
.editor {
  display: flex; flex-direction: column; height: 100%;
  border-radius: var(--radius); overflow: hidden;
  border: 1px solid var(--line);
}
.editor.dark { border-color: #14231f; }
.editor-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px; flex-shrink: 0;
  background: #eef4f2; border-bottom: 1px solid var(--line);
}
.editor.dark .editor-head { background: #11201b; border-color: #14231f; }
.fname { font-size: 12px; color: var(--text-2); opacity: .8; }
.editor.dark .fname { color: #b7d9cd; }
.theme-switch { display: inline-flex; gap: 3px; }
.theme-switch button {
  height: 22px; padding: 0 10px; border: none; border-radius: 6px; cursor: pointer;
  font-size: 11px; font-family: inherit; color: var(--text-dim); background: transparent;
  transition: all .15s ease;
}
.theme-switch button.on { background: var(--primary); color: #fff; }
.editor-box { flex: 1; min-height: 0; }
</style>
