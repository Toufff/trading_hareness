import {mount,flushPromises} from '@vue/test-utils';
import {vi,test,expect,beforeEach} from 'vitest';
import View from './IntradayScanView.vue';
import {getJson} from '../api/http';
vi.mock('../api/http',()=>({getJson:vi.fn()}));
vi.mock('../components/IntradayScanChart.vue',()=>({__esModule:true,default:{template:'<div class="chart-ready" />'}}));
beforeEach(()=>{vi.mocked(getJson).mockReset();});
const item={symbol:'600000.SH',name:'样本股',lane:'accumulation',state:'platform_observation',price:10,reason:'平台未破',matched_today:true,chart:[{time:'1450',close:10,vwap:9.9}]};
const response={status:'completed',run_id:'test',runs:[],result:{lanes:[{key:'accumulation',label:'潜伏',total_matches:1,items:[item]},{key:'event',label:'事件',total_matches:0,items:[]}],market:{symbols:5000,up:3000,down:2000,median:.3},phase:'after_close_initialization'},detail:item,changes:[]};
test('result, details, strategy switching and empty lane stay independent',async()=>{
  vi.mocked(getJson).mockResolvedValue(response);
  const wrapper=mount(View);await flushPromises();
  expect(wrapper.text()).toContain('闭市初始化，不是收盘前建议');
  expect(wrapper.find('.detail').text()).toContain('平台未破');
  const buttons=wrapper.findAll('nav button');expect(buttons.length).toBe(2);await buttons[1]!.trigger('click');await flushPromises();
  expect(wrapper.find('.list').text()).toContain('本策略没有候选');
  expect(wrapper.find('.detail').text()).not.toContain('平台未破');wrapper.unmount();
});
test('request failures visible rather than empty success',async()=>{
  vi.mocked(getJson).mockImplementation(async()=>{await Promise.resolve();throw new Error('upstream failed');});
  const wrapper=mount(View);await flushPromises();expect(wrapper.find('[role="alert"]').text()).toContain('upstream failed');wrapper.unmount();
});
test('no-run state does not claim market or recommendation',async()=>{
  vi.mocked(getJson).mockResolvedValue({status:'no_run',result:null,runs:[]});
  const wrapper=mount(View);await flushPromises();expect(wrapper.text()).toContain('尚无九策略盘中扫描');expect(wrapper.find('.metrics').exists()).toBe(false);wrapper.unmount();
});
test('zero matches with missing history is not presented as a full-market negative',async()=>{
  vi.mocked(getJson).mockResolvedValue({...response,result:{...response.result,lanes:[{key:'contraction',label:'波动收缩',total_matches:0,items:[],status:'partial',data_gaps:{strict_ohlc_40:419}}]}});
  const wrapper=mount(View);await flushPromises();
  expect(wrapper.get('[data-testid="coverage-warning"]').text()).toContain('419只');
  expect(wrapper.text()).toContain('零匹配不代表全市场没有机会');wrapper.unmount();
});
