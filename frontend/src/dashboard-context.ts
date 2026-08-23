import type { InjectionKey } from 'vue';

/**
 * Transitional shell context for independently mounted dashboard views.
 *
 * Views receive only the dashboard setup bindings and never reach into a
 * parent component instance.  The shell remains the owner of polling and
 * mutations; a later slice can narrow a view to typed props without changing
 * its route or rendering contract.
 */
export type DashboardContext = Record<string, unknown>;

export const dashboardContextKey: InjectionKey<DashboardContext> = Symbol('quant-dashboard-context');
