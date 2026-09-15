import {mount,flushPromises} from '@vue/test-utils';
import {describe,it,expect,vi,afterEach} from 'vitest';
import EventResearchLive from './EventResearchLive.vue';
import {getJson} from '../api/http';
vi.mock('../api/http',()=>({getJson:vi.fn()}));
afterEach(()=>{vi.useRealTimers();vi.clearAllMocks();});
describe('live news delivery',()=>{
  it('refreshes visible analysis and stops polling after unmount',async()=>{
    vi.useFakeTimers();
    vi.mocked(getJson).mockResolvedValue({status:'analyzed',summary:'首轮消息',events:[],leads:[]});
    const w=mount(EventResearchLive);await flushPromises();expect(w.text()).toContain('首轮消息');
    vi.mocked(getJson).mockResolvedValue({status:'analyzed',summary:'午盘新消息',events:[],leads:[]});
    await vi.advanceTimersByTimeAsync(60_000);await flushPromises();expect(w.text()).toContain('午盘新消息');
    w.unmount();const count=vi.mocked(getJson).mock.calls.length;
    await vi.advanceTimersByTimeAsync(120_000);expect(getJson).toHaveBeenCalledTimes(count);
  });
});
