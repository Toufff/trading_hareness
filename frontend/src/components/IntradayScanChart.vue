<script setup lang="ts">
import VChart from 'vue-echarts';
import { use } from 'echarts/core';
import { LineChart, CandlestickChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent, MarkLineComponent, DataZoomComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';
use([LineChart,CandlestickChart,GridComponent,LegendComponent,TooltipComponent,MarkLineComponent,DataZoomComponent,CanvasRenderer]);
defineProps<{option:EChartsOption}>();
</script>
<template><VChart :option="option" autoresize /></template>
