<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import PersonalDecisionView from './PersonalDecisionView.vue';
import WorkspaceHeader from '../components/WorkspaceHeader.vue';

function decisionPath() {
  return window.location.pathname.replace(/\/$/, '') === '/holdings' ? '/holdings' : '/market';
}

const activeDecisionPath = ref(decisionPath());
const mode = computed<'market' | 'holdings'>(() => activeDecisionPath.value === '/holdings' ? 'holdings' : 'market');

function navigate(event: MouseEvent, target: string) {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
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
      <WorkspaceHeader :active="mode" @navigate="navigate" />
      <main class="decision-content">
        <PersonalDecisionView :key="mode" :mode="mode" />
      </main>
    </div>
  </el-config-provider>
</template>

<style scoped>.decision-shell { min-height:100vh; background:var(--gs-ground); color:var(--gs-ink); }</style>
