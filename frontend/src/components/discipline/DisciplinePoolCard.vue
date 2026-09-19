<script setup lang="ts">
/**
 * "纪律卡" expander for one recommendation-pool pick: finds the newest new-buy discipline plan of the
 * symbol and renders it with the same DisciplineDetail the holdings page uses.  Read-only.
 */
import { computed, ref } from 'vue';
import DisciplineDetail from './DisciplineDetail.vue';
import { ALL_STATUSES, errorText, fetchLatestPlans } from './discipline-api';
import { shanghaiToday } from './discipline-model';
import type { DisciplinePlan, PlansResponse } from './types';

const props = defineProps<{ symbol: string; name?: string; accountKey?: string }>();

// One read per account per page view, shared by every pick's expander.
const cache = new Map<string, Promise<PlansResponse>>();

const open = ref(false);
const loading = ref(false);
const error = ref('');
const plan = ref<DisciplinePlan | null>(null);
const loaded = ref(false);
const account = computed(() => props.accountKey || localStorage.getItem('personal-decision-account') || 'citics-primary');

async function load(force = false) {
  loading.value = true;
  error.value = '';
  try {
    if (force) cache.delete(account.value);
    if (!cache.has(account.value)) cache.set(account.value, fetchLatestPlans(account.value, ALL_STATUSES));
    const response = await cache.get(account.value)!;
    plan.value = response.items
      .filter((item) => item.symbol === props.symbol && item.plan_kind === 'new_buy')
      .sort((a, b) => b.as_of_at.localeCompare(a.as_of_at))[0] ?? null;
    loaded.value = true;
  } catch (cause) {
    cache.delete(account.value);
    error.value = `纪律卡读取失败：${errorText(cause)}`;
  } finally {
    loading.value = false;
  }
}

function toggle() {
  open.value = true;
  if (!loaded.value) void load();
}
</script>

<template>
  <div
    class="pool-discipline"
    :data-symbol="symbol"
  >
    <button
      type="button"
      class="toggle"
      :aria-expanded="open"
      @click="toggle"
    >
      纪律卡
    </button>
    <!-- A pick card is too narrow for a K-line; the card opens full width in a drawer. -->
    <el-drawer
      v-model="open"
      size="min(1240px, 96vw)"
      direction="rtl"
      :title="`${name || symbol}（${symbol}）新买纪律卡`"
      append-to-body
    >
      <div
        class="panel"
        data-testid="pool-discipline-panel"
        :data-symbol="symbol"
      >
        <el-skeleton
          v-if="loading"
          :rows="5"
          animated
        />
        <el-alert
          v-else-if="error"
          type="error"
          :closable="false"
          show-icon
          :title="error"
        >
          <el-button
            size="small"
            @click="load(true)"
          >
            重试
          </el-button>
        </el-alert>
        <el-empty
          v-else-if="loaded && !plan"
          :image-size="44"
          :description="`${name || symbol} 尚未生成新买纪律卡，运行 stock-discipline 生成`"
        />
        <DisciplineDetail
          v-else-if="plan"
          :plan="plan"
          :today="shanghaiToday()"
        />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.pool-discipline { margin-top: 10px; }
.toggle { padding: 5px 12px; border: 1px solid #93c5fd; border-radius: 5px; background: #eff6ff; color: #1d4ed8; cursor: pointer; font: inherit; font-size: 13px; }
.panel { min-height: 200px; }
</style>
