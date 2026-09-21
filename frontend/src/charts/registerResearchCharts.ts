// Loaded only with research/chart views. Keeping this out of the root
// workspace prevents the ECharts renderer chunk from blocking first paint.
import { use } from 'echarts/core';
import { BarChart, CandlestickChart, LineChart, ScatterChart } from 'echarts/charts';
import {
  DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent,
  MarkLineComponent, MarkPointComponent, TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

use([
  BarChart, CandlestickChart, LineChart, ScatterChart, DataZoomComponent,
  GridComponent, LegendComponent, MarkAreaComponent, MarkLineComponent,
  MarkPointComponent, TooltipComponent, CanvasRenderer,
]);
