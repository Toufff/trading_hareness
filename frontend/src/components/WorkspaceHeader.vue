<script setup lang="ts">
defineProps<{ active?: string }>();
const emit = defineEmits<{ navigate: [event: MouseEvent, path: string] }>();
const pages = [
  { path: '/market', label: '选股', key: 'market' },
  { path: '/holdings', label: '持仓', key: 'holdings' },
  { path: '/intraday', label: '盘中', key: 'intraday' },
  { path: '/sector-heat', label: '板块', key: 'sector-heat' },
  { path: '/research', label: '研究', key: 'research' },
  { path: '/agent-paper', label: '模拟盘', key: 'agent-paper' },
];
const tools = [
  { path: '/monitor', label: '导入监控', key: 'monitor' },
  { path: '/workbench', label: '飞书工作台', key: 'workbench' },
  { path: '/relay', label: '手动投递', key: 'relay' },
];
</script>

<template>
  <header class="workspace-header">
    <a class="workspace-brand" href="/market" aria-label="观市 首页" @click="emit('navigate', $event, '/market')">
      <span>观市</span><span class="workspace-seal" aria-label="研究">研</span>
    </a>
    <nav class="workspace-nav" aria-label="主导航">
      <a v-for="page in pages" :key="page.key" :href="page.path" :aria-current="active === page.key ? 'page' : undefined" @click="emit('navigate', $event, page.path)">{{ page.label }}</a>
      <details class="workspace-tools">
        <summary :class="{ selected: tools.some(item => item.key === active) }">工具</summary>
        <nav aria-label="辅助工具">
          <a v-for="page in tools" :key="page.key" :href="page.path" :aria-current="active === page.key ? 'page' : undefined">{{ page.label }}</a>
        </nav>
      </details>
    </nav>
  </header>
</template>

<style scoped>
.workspace-header { display:flex; align-items:center; justify-content:space-between; gap:24px; padding:25px max(24px, calc((100vw - 1440px)/2)); background:var(--gs-sky); color:var(--gs-cloud); border-bottom:3px solid var(--gs-gold); }
.workspace-brand { display:inline-flex; align-items:center; gap:15px; flex-shrink:0; color:inherit; text-decoration:none; font:29px/1.2 var(--gs-title-font); letter-spacing:5px; }
.workspace-seal { display:grid; place-items:center; padding:4px 3px; border:1px solid #d59a84; color:var(--gs-cloud); background:#a44336; font:14px/1 var(--gs-title-font); letter-spacing:0; }
.workspace-nav { display:flex; gap:5px; align-items:center; flex-wrap:wrap; }
.workspace-nav > a, summary { color:var(--gs-cloud); text-decoration:none; font-size:14px; padding:9px 15px; border-radius:3px; cursor:pointer; list-style:none; }
summary::-webkit-details-marker { display:none; }
.workspace-nav > a:hover, summary:hover { background:rgb(246 241 229 / 10%); }
.workspace-nav > a[aria-current=page], summary.selected { color:var(--gs-sky-deep); background:var(--gs-cloud); }
.workspace-tools { position:relative; }
.workspace-tools nav { position:absolute; z-index:50; right:0; top:calc(100% + 8px); min-width:155px; padding:6px; background:var(--gs-paper); border:1px solid var(--gs-line); box-shadow:var(--el-box-shadow-light); }
.workspace-tools nav a { display:block; color:var(--gs-ink); text-decoration:none; padding:12px; font-size:13px; }
.workspace-tools nav a:hover, .workspace-tools nav a[aria-current=page] { background:var(--gs-wash); }
a:focus-visible, summary:focus-visible { outline:2px solid var(--gs-gold); outline-offset:4px; }
@media(max-width:760px) {
  .workspace-header { padding:21px 16px 12px; gap:18px; flex-direction:column; align-items:stretch; }
  .workspace-brand { align-self:flex-start; font-size:27px; }
  .workspace-nav { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:2px; }
  .workspace-nav > a, summary { text-align:center; padding:12px 1px; font-size:12px; white-space:nowrap; }
  .workspace-tools nav { min-width:160px; }
}
</style>
