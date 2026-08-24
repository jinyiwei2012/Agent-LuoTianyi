import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 60040,
    // 开发时代理到 FastAPI 服务端，规避 CORS
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:60030',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:60030',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
