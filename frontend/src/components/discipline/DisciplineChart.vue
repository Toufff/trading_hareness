<script setup lang="ts">
/**
 * The discipline K-line chart.  Lines are drawn only over [plan date, valid_until]; their price tags sit
 * in a right-hand gutter, spread apart with leader lines when they would collide.  Everything drawn is a
 * price the owner API returned (plan JSON / chart payload); nothing is derived here.
 */
import VChart from 'vue-echarts';
import { guanshiChartTheme } from '../../theme/chart-theme';
import { use } from 'echarts/core';
import { BarChart, CandlestickChart, LineChart } from 'echarts/charts';
import {
  DataZoomComponent, GraphicComponent, GridComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent, TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { GRID_RIGHT } from './discipline-chart-option';
import { LINE_STYLE, staggerLabels } from './discipline-model';
import type { HardStopTerms } from './types';

use([CandlestickChart, BarChart, LineChart, GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent,
  MarkAreaComponent, MarkPointComponent, GraphicComponent, CanvasRenderer]);

export type RightLabel = { key: string; price: number; text: string; color: string; current?: boolean };

const props = defineProps<{
  option: EChartsOption;
  labels: RightLabel[];
  hardStopKey?: string | null;
  hardStopTerms?: HardStopTerms | null;
  height?: number;
  gutter?: number;
  activeKey?: string | null;
}>();
const emit = defineEmits<{ (event: 'hover-line', key: string | null): void; (event: 'click-line', key: string): void }>();

type ChartHandle = {
  convertToPixel: (finder: Record<string, number>, value: number) => number;
  getHeight: () => number; getWidth: () => number;
  setOption: (option: Record<string, unknown>, opts?: Record<string, unknown>) => void;
};
const chartRef = ref<ChartHandle | null>(null);
// The reader's zoom survives a re-render (hover highlight, layer toggle): it is re-applied to the next option.
let zoom: { start: number; end: number } | null = null;
const finalOption = computed<EChartsOption>(() => {
  const option = props.option;
  const current = zoom;
  if (!current || !Array.isArray(option.dataZoom)) return option;
  return { ...option, dataZoom: option.dataZoom.map((item) => ({ ...item, start: current.start, end: current.end })) };
});
function onDataZoom() {
  const chart = instance() as unknown as { getOption?: () => { dataZoom?: Array<{ start?: number; end?: number }> } } | null;
  const state = chart?.getOption?.().dataZoom?.[0];
  if (state && typeof state.start === 'number' && typeof state.end === 'number') zoom = { start: state.start, end: state.end };
  placeLabels();
}
const hovered = ref<string | null>(null);
let signature = '';
const LABEL_GAP = 17;

function instance(): ChartHandle | null {
  // The raw ECharts instance: vue-echarts' own setOption wrapper manages the reactive option.
  const holder = chartRef.value as unknown as { chart?: ChartHandle } | null;
  return holder?.chart ?? null;
}

function placeLabels() {
  const chart = instance();
  if (!chart) return;
  const width = chart.getWidth();
  const height = chart.getHeight();
  const top = 30;
  const bottom = height * 0.7;
  const positioned = props.labels.map((label) => {
    let y = Number.NaN;
    try { y = chart.convertToPixel({ yAxisIndex: 0 }, label.price); } catch { y = Number.NaN; }
    return { label, y };
  }).filter((item) => Number.isFinite(item.y) && item.y >= top - 2 && item.y <= bottom + 2);
  const spread = staggerLabels(positioned.map((item) => item.y), LABEL_GAP, top + 6, bottom - 6);
  const x = width - (props.gutter ?? GRID_RIGHT);
  const next = JSON.stringify([width, height, positioned.map((item, index) => [item.label.key, Math.round(item.y), Math.round(spread[index]!)])]);
  if (next === signature) return;
  signature = next;
  const elements = positioned.flatMap((item, index) => {
    const y1 = spread[index]!;
    const color = item.label.color;
    const moved = Math.abs(y1 - item.y) > 1;
    return [
      { type: 'polyline', silent: true, shape: { points: [[x, item.y], [x + 6, item.y], [x + 12, y1], [x + 14, y1]] },
        style: { stroke: color, lineWidth: moved ? 1 : 0.8, opacity: 0.8 } },
      { type: 'text', silent: true, x: x + 16, y: y1, style: {
        text: item.label.text, fill: item.label.current ? '#fff' : color, fontSize: 11,
        fontWeight: item.label.current || item.label.key === props.hardStopKey ? 'bold' : 'normal',
        verticalAlign: 'middle', backgroundColor: item.label.current ? '#1f2937' : 'rgba(255,255,255,0.88)',
        padding: [2, 4], borderRadius: 3 } },
    ];
  });
  chart.setOption({ graphic: [{ id: 'discipline-labels', type: 'group', children: elements }] }, { replaceMerge: ['graphic'] });
  placedCount.value = positioned.length;
}

const placedCount = ref(0);
let timer: ReturnType<typeof setTimeout> | null = null;
function schedulePlacement() {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { timer = null; placeLabels(); }, 60);
}
watch(() => [props.option, props.labels], () => { signature = ''; schedulePlacement(); }, { deep: false, flush: 'post' });
onMounted(schedulePlacement);
onBeforeUnmount(() => { if (timer) clearTimeout(timer); });

type LineEvent = { componentType?: string; data?: unknown };
const lineKeyOf = (params: LineEvent): string | null => {
  const data = params.data as { lineKey?: unknown } | null | undefined;
  return params.componentType === 'markLine' && data && typeof data.lineKey === 'string' ? data.lineKey : null;
};
// Every marker carries its own explanation (``detail``); hovering shows it next to the pointer.
const markerNote = ref<{ text: string; x: number; y: number } | null>(null);
function onMouseOver(params: LineEvent & { event?: { offsetX?: number; offsetY?: number } }) {
  const data = params.data as { detail?: unknown } | null | undefined;
  if (params.componentType === 'markPoint' && data && typeof data.detail === 'string') {
    markerNote.value = { text: data.detail, x: params.event?.offsetX ?? 80, y: params.event?.offsetY ?? 80 };
    return;
  }
  const key = lineKeyOf(params);
  if (key) {
    hovered.value = key;
    emit('hover-line', key);
  }
}
function onMouseOut(params: LineEvent) {
  if (params.componentType === 'markPoint') markerNote.value = null;
  if (params.componentType === 'markLine') {
    hovered.value = null;
    emit('hover-line', null);
  }
}
function onClick(params: LineEvent) {
  const key = lineKeyOf(params);
  if (key) emit('click-line', key);
}

const showTerms = computed(() => Boolean(props.hardStopTerms && props.hardStopKey
  && (hovered.value ?? props.activeKey) === props.hardStopKey));
const termColor = LINE_STYLE.hard.color;
</script>

<template>
  <div
    class="discipline-chart"
    :style="{ height: `${height ?? 460}px` }"
    data-testid="discipline-chart"
    :data-labels="placedCount"
  >
    <VChart
      :theme="guanshiChartTheme"
      ref="chartRef"
      :option="finalOption"
      autoresize
      :update-options="{ notMerge: true }"
      @finished="schedulePlacement"
      @datazoom="onDataZoom"
      @mouseover="onMouseOver"
      @mouseout="onMouseOut"
      @click="onClick"
    />
    <div
      v-if="markerNote"
      class="marker-note"
      role="tooltip"
      data-testid="marker-note"
      :style="{ left: '66px', top: '6px' }"
    >
      {{ markerNote.text }}
    </div>
    <div
      v-if="showTerms && hardStopTerms"
      class="terms-card"
      role="tooltip"
      data-testid="hard-stop-terms"
    >
      <strong :style="{ color: termColor }">硬止损 {{ hardStopTerms.price?.toFixed(2) }} 的推导</strong>
      <code>{{ hardStopTerms.formula }}</code>
      <table>
        <tbody>
          <tr
            v-for="term in hardStopTerms.terms"
            :key="term.term"
            :class="{ binding: term.term === hardStopTerms.binding_term }"
          >
            <td>{{ term.label }}</td><td>{{ term.value === null ? '—' : term.value.toFixed(2) }}</td>
            <td>{{ term.term === hardStopTerms.binding_term ? '起约束' : '' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.discipline-chart { position: relative; width: 100%; min-width: 0; }
.terms-card { position: absolute; left: 66px; top: 36px; z-index: 3; max-width: min(360px, 80%); padding: 10px 12px; border: 1px solid #fecaca;
  border-radius: 8px; background: rgba(255,255,255,.97); box-shadow: 0 6px 18px rgba(15,23,42,.12); font-size: 12px; pointer-events: none; }
.terms-card strong { display: block; margin-bottom: 4px; }
.marker-note { position: absolute; z-index: 6; border-left: 3px solid #dc2626; max-width: 300px; padding: 6px 9px; border: 1px solid #cbd5e1; border-radius: 6px; background: rgba(255,255,255,.97);
  box-shadow: 0 4px 12px rgba(15,23,42,.14); font-size: 12px; line-height: 1.5; white-space: pre-line; pointer-events: none; }
.terms-card code { display: block; margin-bottom: 6px; white-space: normal; word-break: break-all; color: #475569; font-size: 11px; }
.terms-card table { border-collapse: collapse; width: 100%; }
.terms-card td { padding: 2px 6px 2px 0; }
.terms-card tr.binding td { color: #b91c1c; font-weight: 600; }
</style>
