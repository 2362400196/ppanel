<script setup>
// 门户首页：产品展示 + 登录/控制台入口（公开页，无侧边栏壳）
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const logged = computed(() => !!auth.token)

// hero 终端：命令逐字打出 → 日志逐行滚出 → 循环
const LINES = [
  { text: '$ ppanel create --plan pro --env python:3.12-slim', type: 'cmd' },
  { text: '→ 选取节点 … ws1-ubuntu（在线）', type: 'log' },
  { text: '→ 拉取镜像 / 分配端口 / 创建容器 …', type: 'log' },
  { text: '✓ 实例已开通 · 端口 49152 · 用时 41s', type: 'ok' },
]
const typed = ref('')
const shown = ref(0)          // 已滚出的日志行数
const typing = ref(true)
let tm1, tm2

function typeLoop() {
  const cmd = LINES[0].text
  let i = 0
  typing.value = true
  shown.value = 0
  typed.value = ''
  tm1 = setInterval(() => {
    i++
    typed.value = cmd.slice(0, i)
    if (i >= cmd.length) {
      clearInterval(tm1)
      typing.value = false
      tm2 = setInterval(() => {
        shown.value++
        if (shown.value >= LINES.length - 1) {
          clearInterval(tm2)
          setTimeout(typeLoop, 3600)  // 停留后循环
        }
      }, 650)
    }
  }, 55)
}

// 滚动 reveal
let io
onMounted(() => {
  typeLoop()
  io = new IntersectionObserver(es => es.forEach(e => {
    if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target) }
  }), { threshold: 0.15 })
  document.querySelectorAll('.reveal').forEach(el => io.observe(el))
})
onUnmounted(() => { clearInterval(tm1); clearInterval(tm2); io?.disconnect() })
</script>

<template>
  <div class="portal">
    <!-- 顶栏 -->
    <header class="nav">
      <div class="nav-inner">
        <a class="brand" href="/">
          <span class="logo">P</span>
          <span class="brand-name">PPanel</span>
        </a>
        <nav class="nav-links">
          <a href="#features">能力</a>
          <a href="#how">开通流程</a>
        </nav>
        <div class="nav-acts">
          <template v-if="logged">
            <router-link class="btn ghost" to="/instances">控制台</router-link>
          </template>
          <template v-else>
            <router-link class="btn ghost" to="/login">登录</router-link>
            <router-link class="btn solid" to="/login">免费注册</router-link>
          </template>
        </div>
      </div>
    </header>

    <!-- Hero：全幅，文案 + 终端 -->
    <section class="hero">
      <div class="glow g1" />
      <div class="glow g2" />
      <div class="hero-grid">
        <div class="hero-copy">
          <p class="kicker mono">CONTAINER PANEL · 自托管云面板</p>
          <h1 class="title">一键开通<br>秒级交付你的容器</h1>
          <p class="sub">多环境容器托管 · 独立面板与 SSL · 流量与备份管理，注册即用，按套餐计费。</p>
          <div class="hero-acts">
            <router-link class="btn solid lg" to="/shop">立即开始</router-link>
            <a class="btn ghost lg" href="#how">了解开通流程</a>
          </div>
        </div>

        <div class="term-wrap">
          <div class="term">
            <div class="term-bar">
              <i /><i /><i />
              <span class="term-title mono">ppanel — ssh</span>
            </div>
            <div class="term-body mono">
              <p class="t-cmd">{{ typed }}<span v-if="typing" class="caret">▌</span></p>
              <p v-for="(l, i) in LINES.slice(1)" v-show="shown > i" :key="i"
                 class="t-log" :class="{ ok: l.type === 'ok' }">{{ l.text }}</p>
            </div>
          </div>
          <div class="term-shadow" />
        </div>
      </div>
    </section>

    <!-- 能力：无卡片三列 -->
    <section id="features" class="features reveal">
      <div class="sec-inner">
        <div class="feat">
          <p class="feat-no mono">01</p>
          <h3>秒级开通</h3>
          <p>选好套餐自动创建容器，Python / PHP / Node / Go 环境开箱即用，重建不丢数据。</p>
        </div>
        <div class="feat">
          <p class="feat-no mono">02</p>
          <h3>独立面板 · 自动 SSL</h3>
          <p>每个实例配独立管理面板，绑定域名自动签发续期 Let's Encrypt 证书。</p>
        </div>
        <div class="feat">
          <p class="feat-no mono">03</p>
          <h3>备份与稳定</h3>
          <p>数据库与目录一键备份恢复，节点稳定性评分与崩溃自愈全程看护。</p>
        </div>
      </div>
    </section>

    <!-- 开通流程三步 -->
    <section id="how" class="how reveal">
      <div class="sec-inner">
        <h2 class="sec-title">从下单到上线，只要一分钟</h2>
        <div class="steps">
          <div class="step">
            <p class="step-no mono">STEP 01</p>
            <h4>挑选套餐</h4>
            <p>按算力、内存与流量选一档，环境由套餐决定。</p>
          </div>
          <div class="step-line" />
          <div class="step">
            <p class="step-no mono">STEP 02</p>
            <h4>钱包充值</h4>
            <p>微信扫码充值余额，管理员也可直接代充。</p>
          </div>
          <div class="step-line" />
          <div class="step">
            <p class="step-no mono">STEP 03</p>
            <h4>自动开通</h4>
            <p>余额扣款后容器自动创建，立刻进入面板。</p>
          </div>
        </div>
      </div>
    </section>

    <!-- Final CTA -->
    <section class="final">
      <div class="final-inner reveal">
        <h2>现在开始，<br>拥有自己的云容器。</h2>
        <router-link class="btn solid lg invert" to="/shop">免费注册开通</router-link>
      </div>
    </section>

    <footer class="foot">
      <div class="sec-inner foot-inner">
        <span class="brand mini"><span class="logo sm">P</span> PPanel</span>
        <span class="foot-note">容器托管面板 · 秒级交付</span>
        <router-link class="foot-link" to="/login">登录</router-link>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.portal { background: var(--bg); color: var(--text); font-family: inherit; }
.mono { font-family: ui-monospace, Consolas, monospace; }

/* ---- 通用按钮（门户内自洽，不用面板组件） ---- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  border-radius: 999px; font-size: 13.5px; font-weight: 600; text-decoration: none;
  padding: 9px 20px; transition: all .18s ease; cursor: pointer; border: 1px solid transparent;
}
.btn.solid { background: var(--primary-strong, #0f7a63); color: #fff; }
.btn.solid:hover { background: var(--primary-deep, #0b5c4a); transform: translateY(-1px); box-shadow: 0 8px 20px -8px color-mix(in srgb, var(--primary) 55%, transparent); }
.btn.ghost { border-color: var(--line); color: var(--text-2); background: transparent; }
.btn.ghost:hover { border-color: var(--primary); color: var(--primary-strong); }
.btn.lg { padding: 12px 28px; font-size: 14.5px; }
.btn.invert { background: #fff; color: var(--primary-deep, #0b5c4a); }
.btn.invert:hover { transform: translateY(-1px); box-shadow: 0 10px 26px -10px rgba(0,0,0,.4); }

/* ---- 顶栏 ---- */
.nav { position: sticky; top: 0; z-index: 20; backdrop-filter: blur(14px); background: color-mix(in srgb, var(--bg) 82%, transparent); border-bottom: 1px solid color-mix(in srgb, var(--line) 70%, transparent); }
.nav-inner { max-width: 1120px; margin: 0 auto; padding: 0 24px; height: 60px; display: flex; align-items: center; gap: 28px; }
.brand { display: flex; align-items: center; gap: 9px; text-decoration: none; color: var(--text); }
.logo {
  width: 30px; height: 30px; border-radius: 9px; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, var(--primary), var(--primary-deep, #0b5c4a));
  color: #fff; font-weight: 800; font-size: 16px; box-shadow: 0 4px 12px -4px color-mix(in srgb, var(--primary) 60%, transparent);
}
.logo.sm { width: 22px; height: 22px; font-size: 12px; border-radius: 7px; }
.brand-name { font-weight: 800; font-size: 16.5px; letter-spacing: .2px; }
.brand.mini { font-size: 13.5px; font-weight: 700; gap: 7px; }
.nav-links { display: flex; gap: 22px; margin-left: 8px; }
.nav-links a { font-size: 13.5px; color: var(--text-2); text-decoration: none; transition: color .15s; }
.nav-links a:hover { color: var(--primary-strong); }
.nav-acts { margin-left: auto; display: flex; gap: 10px; }

/* ---- Hero ---- */
.hero { position: relative; overflow: hidden; min-height: calc(100svh - 60px); display: flex; align-items: center; }
.glow { position: absolute; border-radius: 50%; filter: blur(90px); opacity: .5; pointer-events: none; }
.g1 { width: 520px; height: 520px; right: -120px; top: -140px; background: radial-gradient(circle, color-mix(in srgb, var(--primary) 26%, transparent), transparent 70%); animation: float1 11s ease-in-out infinite; }
.g2 { width: 420px; height: 420px; left: -140px; bottom: -160px; background: radial-gradient(circle, color-mix(in srgb, var(--primary) 18%, transparent), transparent 70%); animation: float2 13s ease-in-out infinite; }
@keyframes float1 { 50% { transform: translate(-40px, 34px) scale(1.06); } }
@keyframes float2 { 50% { transform: translate(36px, -30px) scale(1.05); } }

.hero-grid {
  position: relative; max-width: 1120px; margin: 0 auto; padding: 48px 24px 64px;
  display: grid; grid-template-columns: 1.05fr 1fr; gap: 48px; align-items: center; width: 100%;
}
.kicker { font-size: 11.5px; letter-spacing: 2.5px; color: var(--primary-strong); font-weight: 700; margin: 0 0 18px; }
.title { font-size: clamp(38px, 5.2vw, 60px); line-height: 1.12; margin: 0 0 18px; font-weight: 800; letter-spacing: -1px; }
.sub { font-size: 15.5px; line-height: 1.8; color: var(--text-2); margin: 0 0 30px; max-width: 460px; }
.hero-acts { display: flex; gap: 12px; flex-wrap: wrap; }

/* 终端窗口 */
.term-wrap { position: relative; }
.term {
  position: relative; z-index: 2; border-radius: 14px; overflow: hidden;
  background: #0e1a17; box-shadow: 0 30px 70px -30px rgba(9, 60, 48, .55);
  border: 1px solid rgba(255,255,255,.06);
}
.term-bar { display: flex; align-items: center; gap: 7px; padding: 11px 14px; background: rgba(255,255,255,.045); }
.term-bar i { width: 11px; height: 11px; border-radius: 50%; background: #2e4a42; }
.term-bar i:first-child { background: #ff5f57; } .term-bar i:nth-child(2) { background: #febc2e; } .term-bar i:nth-child(3) { background: #28c840; }
.term-title { margin-left: 8px; font-size: 11px; color: #7fa89d; letter-spacing: 1px; }
.term-body { padding: 18px 18px 22px; font-size: 12.8px; line-height: 2.05; min-height: 168px; }
.t-cmd { color: #d7efe7; margin: 0; }
.t-log { color: #8fb8ab; margin: 0; animation: rise .4s ease both; }
.t-log.ok { color: #4ade9d; font-weight: 600; }
.caret { color: #4ade9d; animation: blink 1s steps(1) infinite; }
@keyframes blink { 50% { opacity: 0; } }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } }
.term-shadow { position: absolute; z-index: 1; inset: 26px -16px -18px 16px; border-radius: 16px; background: color-mix(in srgb, var(--primary) 14%, transparent); filter: blur(20px); }

/* ---- 通用节 ---- */
.sec-inner { max-width: 1120px; margin: 0 auto; padding: 0 24px; }
.features { padding: 88px 0 8px; }
.features .sec-inner { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; }
.feat { padding: 10px 34px 10px 0; }
.feat + .feat { border-left: 1px solid var(--line); padding-left: 34px; }
.feat-no { color: var(--primary-strong); font-size: 12px; font-weight: 700; letter-spacing: 2px; margin: 0 0 10px; }
.feat h3 { font-size: 17px; margin: 0 0 10px; font-weight: 700; }
.feat p { font-size: 13.5px; line-height: 1.85; color: var(--text-2); margin: 0; }

.how { padding: 96px 0; }
.sec-title { font-size: 26px; font-weight: 800; letter-spacing: -.5px; margin: 0 0 40px; text-align: center; }
.steps { display: flex; align-items: stretch; gap: 18px; }
.step { flex: 1; }
.step-no { font-size: 11px; letter-spacing: 2px; color: var(--primary-strong); font-weight: 700; margin: 0 0 10px; }
.step h4 { font-size: 16.5px; margin: 0 0 8px; font-weight: 700; }
.step p { font-size: 13.5px; line-height: 1.8; color: var(--text-2); margin: 0; }
.step-line { width: 42px; align-self: center; border-top: 1px dashed color-mix(in srgb, var(--primary) 40%, transparent); margin-top: -34px; }

/* ---- Final CTA ---- */
.final { background: linear-gradient(135deg, #0d2b23, var(--primary-deep, #0b5c4a)); color: #fff; padding: 96px 0; }
.final-inner { text-align: center; }
.final h2 { font-size: clamp(28px, 4vw, 42px); font-weight: 800; letter-spacing: -.5px; line-height: 1.25; margin: 0 0 30px; }

/* ---- 页脚 ---- */
.foot { padding: 26px 0; border-top: 1px solid var(--line); }
.foot-inner { display: flex; align-items: center; gap: 16px; }
.foot-note { font-size: 12.5px; color: var(--text-dim); }
.foot-link { margin-left: auto; font-size: 12.5px; color: var(--text-2); text-decoration: none; }
.foot-link:hover { color: var(--primary-strong); }

/* ---- 滚动 reveal ---- */
.reveal { opacity: 0; transform: translateY(26px); transition: opacity .7s ease, transform .7s ease; }
.reveal.in { opacity: 1; transform: none; }

@media (max-width: 860px) {
  .hero-grid { grid-template-columns: 1fr; gap: 36px; padding-top: 36px; }
  .hero { min-height: 0; }
  .features .sec-inner { grid-template-columns: 1fr; gap: 30px; }
  .feat + .feat { border-left: none; border-top: 1px solid var(--line); padding-left: 0; padding-top: 30px; }
  .steps { flex-direction: column; }
  .step-line { display: none; }
  .nav-links { display: none; }
}
</style>
