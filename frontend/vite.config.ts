import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import Components from 'unplugin-vue-components/vite';
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers';

// The dashboard adapter (feishu-adapter, 127.0.0.1:5680) serves every path the
// frontend calls at runtime -- including the `/api/*` routes it proxies on to
// the quant-service API (127.0.0.1:5681). The frontend never talks to 5681
// directly, so `npm run dev` only needs to forward to the adapter.
const adapterTarget = process.env.VITE_DEV_ADAPTER_TARGET ?? 'http://127.0.0.1:5680';

export default defineConfig({
  plugins: [
    vue(),
    // Auto-registers only the `<el-*>` components each SFC actually uses
    // (with their matching per-component CSS) instead of the previous
    // `app.use(ElementPlus)` full-library install in main.ts.
    Components({
      resolvers: [ElementPlusResolver()],
      dts: false,
    }),
  ],
  server: {
    proxy: {
      '/api': { target: adapterTarget, changeOrigin: true },
      '/events': { target: adapterTarget, changeOrigin: true, ws: true },
      '/manual-relay': { target: adapterTarget, changeOrigin: true },
      '/health': { target: adapterTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          vue: ['vue'],
          'element-plus': ['element-plus', '@element-plus/icons-vue'],
          charts: ['echarts', 'vue-echarts'],
        },
      },
    },
  },
});
