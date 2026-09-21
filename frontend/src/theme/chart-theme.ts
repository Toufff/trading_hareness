// Canvas cannot resolve CSS variables. Keep these in sync with tokens.css.
export const chartColors = {
  ink: '#263e48', muted: '#626d6d', line: '#dcd6c5', paper: '#faf7ef',
  sky: '#2d627c', up: '#ae473c', down: '#387b66', gold: '#836026', purple: '#79617d',
};
const axis = {
  axisLine: { lineStyle: { color: chartColors.line } },
  axisTick: { lineStyle: { color: chartColors.line } },
  axisLabel: { color: chartColors.muted, fontSize: 12, hideOverlap: true },
  splitLine: { lineStyle: { color: chartColors.line, opacity: 0.7 } },
  nameTextStyle: { color: chartColors.muted },
};
export const guanshiChartTheme = {
  color: [chartColors.sky, chartColors.gold, chartColors.down, chartColors.up, chartColors.purple],
  backgroundColor: 'transparent',
  textStyle: { color: chartColors.ink, fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
  title: { textStyle: { color: chartColors.ink }, subtextStyle: { color: chartColors.muted } },
  legend: { textStyle: { color: chartColors.muted } },
  tooltip: { backgroundColor: chartColors.paper, borderColor: chartColors.line, textStyle: { color: chartColors.ink } },
  categoryAxis: axis, valueAxis: axis,
  dataZoom: { backgroundColor: '#f1eee4', fillerColor: 'rgba(45,98,124,0.12)', borderColor: chartColors.line, textStyle: { color: chartColors.muted }, handleStyle: { color: chartColors.sky } },
  candlestick: { itemStyle: { color: chartColors.up, color0: chartColors.down, borderColor: chartColors.up, borderColor0: chartColors.down } },
};
