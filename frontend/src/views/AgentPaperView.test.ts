import {mount,flushPromises} from '@vue/test-utils';
import {vi,test,expect,beforeEach} from 'vitest';
import View from './AgentPaperView.vue';
import {getJson} from '../api/http';
vi.mock('../api/http',()=>({getJson:vi.fn()}));
beforeEach(()=>{vi.mocked(getJson).mockReset();});
test('shows agent and reconstructed human equity with orders and failed decisions',async()=>{
  vi.mocked(getJson).mockResolvedValue({status:'ok',account_key:'agent-claude-opus',model:'claude-sonnet-5',start_date:'2026-09-17',initial_equity:101000,cash:5000,day:'2026-09-17',
    latest_nav:{as_of:'2026-09-17T06:00:00Z',equity:'102010.00',return_pct:1,price_basis:'live_quote'},
    daily:[{trading_date:'2026-09-17',agent_equity:102010,agent_return_pct:1,human_equity:100495,human_return_pct:-0.5,human_fills_imported_through:'2026-09-16',human_missing_prices:[]}],
    positions:[{symbol:'600664.SH',name:'哈药股份',quantity:2300,sellable_quantity:2300,average_cost:'6.9484',realized_pnl:'0'}],
    orders:[{placed_at:'2026-09-17T01:40:00Z',symbol:'600664.SH',name:'哈药股份',side:'sell',order_type:'limit',quantity:1000,limit_price:'7.60',status:'open',filled_quantity:0,fill_price:null,fees:'0',reason:'前高压力位预挂',reject_reasons:[]}],
    decisions_today:{decided:3,failed:1},recent_decisions:[{decided_at:'2026-09-17T01:35:00Z',status:'model_failed',market_view:null,notes:null,order_count:null,error:'timeout: 240s',duration_ms:null}],
    comparison_note:'人类收益=重建'});
  const wrapper=mount(View);await flushPromises();
  expect(wrapper.text()).toContain('102010.00');expect(wrapper.text()).toContain('-0.50%');
  expect(wrapper.text()).toContain('挂单中');expect(wrapper.text()).toContain('限7.60');
  expect(wrapper.text()).toContain('失败：timeout: 240s');wrapper.unmount();
});
test('uninitialized account and request failures are explicit',async()=>{
  vi.mocked(getJson).mockResolvedValueOnce({status:'not_configured',account_key:'agent-claude-opus'});
  const wrapper=mount(View);await flushPromises();expect(wrapper.text()).toContain('尚未初始化');wrapper.unmount();
  vi.mocked(getJson).mockRejectedValueOnce(new Error('upstream failed'));
  const failed=mount(View);await flushPromises();expect(failed.find('[role="alert"]').text()).toContain('upstream failed');failed.unmount();
});
