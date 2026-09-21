import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import WorkspaceHeader from './WorkspaceHeader.vue';

describe('观市 navigation', () => {
  it('keeps every major page and operational destination accessible without an English subtitle', () => {
    const wrapper = mount(WorkspaceHeader, { props: { active: 'holdings' } });
    expect(wrapper.text()).toContain('观市');
    expect(wrapper.text()).not.toContain('StockPlatform');
    expect(wrapper.find('a[aria-current="page"]').attributes('href')).toBe('/holdings');
    for (const route of ['/market', '/holdings', '/intraday', '/sector-heat', '/research', '/agent-paper', '/monitor', '/workbench', '/relay']) {
      expect(wrapper.find(`a[href="${route}"]`).exists()).toBe(true);
    }
  });
  it('emits navigation without suppressing native links on its own', async () => {
    const wrapper = mount(WorkspaceHeader);
    await wrapper.find('a[href="/holdings"]').trigger('click');
    expect(wrapper.emitted('navigate')?.[0]?.[1]).toBe('/holdings');
  });
});
