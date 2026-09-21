<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import PersonalDecisionView from './PersonalDecisionView.vue';

function decisionPath() {
  return window.location.pathname.replace(/\/$/, '') === '/holdings' ? '/holdings' : '/market';
}

const activeDecisionPath = ref(decisionPath());
const mode = computed<'market' | 'holdings'>(() => activeDecisionPath.value === '/holdings' ? 'holdings' : 'market');

const navItems = [
  { path: '/market', label: '市场与选股' },
  { path: '/holdings', label: '我的持仓' },
  { path: '/research', label: '量化研究台' },
  { path: '/monitor', label: '导入监控' },
  { path: '/workbench', label: '飞书工作台' },
  { path: '/relay', label: '手动投递' },
] as const;

function navigate(event: MouseEvent, target: string) {
  if (target !== '/market' && target !== '/holdings') return;
  event.preventDefault();
  if (activeDecisionPath.value === target) return;
  window.history.pushState(null, '', target);
  activeDecisionPath.value = target;
}

function syncFromHistory() {
  activeDecisionPath.value = decisionPath();
}

onMounted(() => window.addEventListener('popstate', syncFromHistory));
onBeforeUnmount(() => window.removeEventListener('popstate', syncFromHistory));
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div class="decision-shell">
      <header class="decision-header">
        <a class="decision-brand" href="/market" aria-label="StockPlatform 市场与选股">
          <strong>StockPlatform</strong>
          <span>个人决策工作台</span>
        </a>
        <nav class="decision-nav" aria-label="主导航">
          <a
            v-for="item in navItems"
            :key="item.path"
            :href="item.path"
            :class="{ active: activeDecisionPath === item.path }"
            @click="navigate($event, item.path)"
          >{{ item.label }}</a>
        </nav>
      </header>
      <main class="decision-content">
        <PersonalDecisionView :mode="mode" />
      </main>
    </div>
  </el-config-provider>
</template>

<style scoped>
.decision-shell {
  min-height: 100vh;
  background: #f4f7fb;
  color: #172033;
}

.decision-header {
  position: sticky;
  z-index: 20;
  top: 0;
  display: flex;
  min-height: 64px;
  align-items: center;
  justify-content: space-between;
  gap: 28px;
  padding: 0 28px;
  border-bottom: 1px solid #dfe6ef;
  background: rgb(255 255 255 / 94%);
  backdrop-filter: blur(12px);
}

.decision-brand {
  display: flex;
  flex: 0 0 auto;
  align-items: baseline;
  gap: 10px;
  color: #172033;
  text-decoration: none;
}

.decision-brand strong {
  font-size: 18px;
  letter-spacing: -.02em;
}

.decision-brand span {
  color: #748198;
  font-size: 12px;
}

.decision-nav {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  scrollbar-width: none;
}

.decision-nav a {
  position: relative;
  flex: 0 0 auto;
  padding: 21px 12px 19px;
  color: #59677d;
  font-size: 14px;
  text-decoration: none;
  transition: color .16s ease;
}

.decision-nav a:hover,
.decision-nav a.active {
  color: #155eef;
}

.decision-nav a.active::after {
  position: absolute;
  right: 12px;
  bottom: 0;
  left: 12px;
  height: 3px;
  border-radius: 3px 3px 0 0;
  background: #155eef;
  content: '';
}

.decision-content {
  padding: 22px 24px 40px;
}

@media (max-width: 820px) {
  .decision-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
    padding: 12px 16px 0;
  }

  .decision-brand span {
    display: none;
  }

  .decision-nav {
    width: 100%;
  }

  .decision-nav a {
    padding: 12px 10px 10px;
  }

  .decision-content {
    padding: 14px 10px 28px;
  }
}
</style>
