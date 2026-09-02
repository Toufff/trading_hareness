import { mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { describe, expect, it } from 'vitest';

import ResearchOnlyBadge from './ResearchOnlyBadge.vue';

describe('ResearchOnlyBadge', () => {
  it('renders a fixed, accessible research-only note', () => {
    const wrapper = mount(ResearchOnlyBadge, { global: { plugins: [ElementPlus] } });

    const note = wrapper.get('[role="note"]');
    expect(note.text()).toContain('仅供研究参考');
    expect(note.text()).toContain('非交易建议');
  });

  it('exposes no configurable props, so the wording is identical at every call site', () => {
    expect(ResearchOnlyBadge.props).toBeFalsy();
  });
});
