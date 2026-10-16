import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: { proxy: { '/api': loadEnv(mode, '.', 'FINAGENT_').FINAGENT_API_PROXY_TARGET || 'http://127.0.0.1:8000' } },
}));
