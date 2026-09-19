<script setup lang="ts">
/** Every discipline line of a plan in plain Chinese, linked to the chart (hover = highlight, click = focus). */
import { computed } from 'vue';
import { KIND_LABEL, LINE_STATE_LABEL, distance, num, price2 } from './discipline-model';
import type { DisciplinePlan } from './types';

const props = defineProps<{
  plan: DisciplinePlan;
  price: number | null;
  atr14: number | null;
  states: Map<number, string>;
  keyOf: (index: number) => string | null;
  highlightKey?: string | null;
  visibleKeys: Set<string>;
}>();
const emit = defineEmits<{
  (event: 'hover', key: string | null): void;
  (event: 'focus', payload: { key: string; price: number }): void;
  (event: 'toggle', key: string): void;
}>();

const EXECUTE_AT: Record<string, string> = { 'next_open+15m': '下一交易日开盘15分钟内' };

const rows = computed(() => props.plan.lines.map((line, index) => {
  const level = num(line.price);
  const gap = line.execute_by === 'time' && line.kind !== 'time_stop' ? null : distance(props.price, level, props.atr14);
  const state = props.states.get(index) ?? 'armed';
  const basis = line.execute_by === 'time'
    ? `按时间：${EXECUTE_AT[line.execute_at ?? ''] ?? line.execute_at ?? '—'}`
    : line.confirm?.basis === 'minute' ? `分钟（连续${line.confirm?.bars ?? 1}根）` : '日线收盘';
  return {
    index, line, level, key: props.keyOf(index), kind: KIND_LABEL[line.kind] ?? line.kind,
    gap, basis, state, stateText: LINE_STATE_LABEL[state] ?? state,
  };
}));

function focus(row: { key: string | null; level: number | null }) {
  if (row.key && row.level !== null) emit('focus', { key: row.key, price: row.level });
}
</script>

<template>
  <div class="line-table-wrap">
    <table
      class="line-table"
      data-testid="discipline-line-table"
    >
      <thead>
        <tr><th>显示</th><th>类型</th><th>条件（人话）</th><th>价格</th><th>距现价</th><th>执行口径</th><th>状态</th><th>优先级</th></tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="row.index"
          :data-kind="row.line.kind"
          :class="{ highlighted: row.key && row.key === highlightKey, clickable: row.level !== null, [`state-${row.state}`]: true }"
          @mouseenter="emit('hover', row.key)"
          @mouseleave="emit('hover', null)"
          @click="focus(row)"
        >
          <td>
            <input
              v-if="row.key"
              type="checkbox"
              :checked="visibleKeys.has(row.key)"
              :aria-label="`在图上显示${row.kind}`"
              @click.stop
              @change="emit('toggle', row.key!)"
            >
          </td>
          <td class="kind">
            <span :class="`kind-${row.line.kind}`">{{ row.kind }}</span>
          </td>
          <td class="condition">
            {{ row.line.label }}
          </td>
          <td class="num">
            {{ row.level === null ? (row.line.action.value !== null && row.line.action.value !== undefined ? `${row.line.action.value} 股` : '—') : price2(row.level) }}
          </td>
          <td
            class="num"
            :class="{ near: row.gap && Math.abs(row.gap.pct) < 3 }"
          >
            {{ row.gap?.text ?? '—' }}
          </td>
          <td>{{ row.basis }}</td>
          <td>
            <span
              class="state"
              :class="`state-${row.state}`"
            >{{ row.stateText }}</span>
          </td>
          <td class="num">
            {{ row.line.priority }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.line-table-wrap { overflow-x: auto; }
.line-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.line-table th, .line-table td { padding: 6px 8px; border-bottom: 1px solid var(--el-border-color-lighter); text-align: left; vertical-align: top; }
.line-table th { color: var(--el-text-color-secondary); font-weight: 500; white-space: nowrap; background: var(--el-fill-color-light); }
.line-table tr.clickable { cursor: pointer; }
.line-table tr.highlighted td { background: #fff7ed; }
.line-table td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
.line-table td.near { color: #b91c1c; font-weight: 600; }
.line-table td.condition { min-width: 260px; line-height: 1.5; }
.kind span { white-space: nowrap; font-weight: 600; }
.kind-hard_stop { color: #d93026; }
.kind-soft_stop { color: #f08c00; }
.kind-trail, .kind-trigger { color: #1a9e5a; }
.kind-take_partial { color: #8e44ad; }
.kind-chase_cap { color: #3f9d6d; }
.state { padding: 1px 6px; border-radius: 4px; background: var(--el-fill-color); white-space: nowrap; }
.state.state-triggered { background: #fee2e2; color: #b91c1c; }
.state.state-capped { background: #fef3c7; color: #92400e; }
.state.state-expired, .state.state-cancelled { color: var(--el-text-color-secondary); }
</style>
