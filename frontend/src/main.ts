import { createApp } from 'vue';
import App from './App.vue';
import './style.css';
import { setDashboardKey } from './api/http';

// Element Plus is no longer installed as a full plugin (`app.use(ElementPlus)`
// used to register and bundle every component, ~873 KB before gzip). Each
// `<el-*>` tag is now auto-imported per-file by unplugin-vue-components (see
// vite.config.ts), which also pulls in only that component's CSS -- so there
// is no longer a single `element-plus/dist/index.css` import here either.
// The zh-CN locale is set once via <el-config-provider> in App.vue.

// One-time operator key handoff: a link like `/?dashboard_key=...` writes the
// key to localStorage for this browser and is then scrubbed from the URL bar
// so it does not linger in history/bookmarks. The persisted key is what
// actually powers every later write request (see src/api/http.ts).
const startupParams = new URLSearchParams(window.location.search);
const startupDashboardKey = startupParams.get('dashboard_key');
if (startupDashboardKey) {
  setDashboardKey(startupDashboardKey);
  startupParams.delete('dashboard_key');
  const remaining = startupParams.toString();
  window.history.replaceState(null, '', `${window.location.pathname}${remaining ? `?${remaining}` : ''}${window.location.hash}`);
}

createApp(App).mount('#app');
