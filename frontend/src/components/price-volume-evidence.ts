export type PriceVolumeEvidence = {
  version: string; status: string; quality_confirmed?: boolean; scope?: string;
  metrics?: Record<string, number | boolean | null>; references?: Record<string, number | null>;
  reasons?: string[]; warnings?: string[]; unverified?: string[];
};
const labels: Record<string, string> = {
  amount_multiple:'成交额相对均值倍数',volume_multiple:'成交股数相对均值倍数',close_location:'收盘在全天区间的位置',
  upper_wick_fraction:'上影占全天波幅',close_return10_pct:'十日收盘涨跌幅',advance_sessions:'上涨段交易日数',
  pullback_sessions:'回调段交易日数',advance_mean_amount:'上涨段日均成交额',pullback_mean_amount:'回调段日均成交额',
  pullback_amount_ratio:'回调与上涨段成交额比',pullback_amount_contracted:'回调成交额是否收缩',
  up_day_mean_amount:'上涨日日均成交额',down_day_mean_amount:'下跌日日均成交额',down_up_amount_ratio:'下跌日与上涨日成交额比',
  first_close_breakout:'是否首次收盘突破',recovery_fraction:'前日跌幅收复比例',latest_change:'当日涨跌幅',amount_expansion:'成交额放大倍数',
  close_platform_high_5d:'前五日收盘平台上沿',recent_close_low:'近期收盘低点',prior10_high:'前十日最高价',
  prior10_low:'前十日最低价',reclaim_reference:'修复参考位',panic_low:'恐慌日最低价',
  intraday_order:'日内触发先后顺序',execution:'真实成交与可成交性',institutional_identity:'真实机构身份',
  daily_ohlc_quality:'当日开高低收完整性',unrestricted_daily_range:'涨跌停限制下的完整供需',
  share_volume_multiple:'成交股数放大倍数',advance_pullback_segments:'完整上涨与回调分段',
  pullback_stabilization:'回调后的止跌确认',long_horizon_position:'长期历史位置',limit_board_process:'封板、开板和回封过程',
};
export const priceVolumeLabel = (key: string): string => labels[key] ?? `未映射证据（${key}）`;
export function priceVolumeValue(key: string, value: number | boolean | null): string {
  if (value == null || (typeof value === 'number' && !Number.isFinite(value))) return '缺少数据';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (key.endsWith('_mean_amount')) return `${(value / 1e8).toFixed(2)} 亿元`;
  if (['close_location','upper_wick_fraction','recovery_fraction'].includes(key)) return `${(value * 100).toFixed(1)}%`;
  if (key.endsWith('_pct') || key === 'latest_change') return `${value.toFixed(2)}%`;
  if (key.endsWith('_sessions')) return `${value} 日`;
  return value.toFixed(2);
}
