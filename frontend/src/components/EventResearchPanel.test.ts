import {mount} from '@vue/test-utils';
import {describe,it,expect} from 'vitest';
import EventResearchPanel from './EventResearchPanel.vue';
describe('event research publication',()=>{
  it('distinguishes citation failure from connectivity failure',()=>{
    const w=mount(EventResearchPanel,{props:{value:{status:'leads_only',events:[],leads:[],
      analysis:{status:'failed',failure_stage:'citation_repair',failure_code:'invalid_citations'}}}});
    expect(w.text()).toContain('失败位置：引用复核');
    expect(w.text()).toContain('invalid_citations');
    expect(w.text()).toContain('不等于模型无法连接');
  });
  it('explains holiday endpoints and does not pretend unknown calendar has a target',()=>{
    const w=mount(EventResearchPanel,{props:{value:{status:'analyzed',events:[],leads:[],
      schedule:{last_expected_at:null,next_expected_at:null,state:'calendar_unavailable',calendar:{status:'missing',reason:'2027未核验'}}}}});
    expect(w.text()).toContain('复市前一日 22:00');
    expect(w.text()).not.toContain('周日午');
    expect(w.text()).toContain('2027未核验');
    expect(w.text()).toContain('下一目标 未知');
  });
  it('renders delayed data and distinguishes failure from no news',async()=>{
    const w=mount(EventResearchPanel,{props:{value:undefined}});
    expect(w.find('[data-testid="event-research"]').exists()).toBe(false);
    await w.setProps({value:{status:'failed',summary:'采集失败，不代表无利好',events:[],leads:[]}});
    expect(w.text()).toContain('消息链失败');
    await w.setProps({value:{status:'no_news',summary:'没有返回',events:[],leads:[]}});
    expect(w.text()).toContain('来源没有返回消息');expect(w.text()).not.toContain('消息链失败');
  });
  it('escapes malicious headline and shows stale status',()=>{
    const w=mount(EventResearchPanel,{props:{value:{status:'leads_only',stale:true,events:[],
      leads:[{document_id:'a',title:'<img src=x onerror=alert(1)>',published_at:'now',symbols:[]}]}}});
    expect(w.find('img').exists()).toBe(false);expect(w.text()).toContain('已过期');
    expect(w.text()).toContain('非已核查利好');
  });
});
