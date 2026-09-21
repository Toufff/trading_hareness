import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import Components from 'unplugin-vue-components/vite';
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers';

// Most development routes use the dashboard adapter; the standalone sector
// page uses the owner's v1 endpoint, matching the public gateway route.
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
      // Standalone board UI uses the owner v1 route (not an adapter /api/research alias).
      '/api/v1/sector-heat': { target: 'http://127.0.0.1:5681', changeOrigin: true },
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
          // Let Rollup split the actually used components. Importing the root
          // libraries here pulled ~1.5 MB into every standalone page startup.
        },
      },
    },
  },
});
