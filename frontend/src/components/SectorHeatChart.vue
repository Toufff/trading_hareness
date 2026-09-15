<script setup lang="ts">
import VChart from 'vue-echarts';
import { use } from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';
use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);
defineProps<{ option: EChartsOption }>();
</script>
<template><VChart :option="option" autoresize /></template>
