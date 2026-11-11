import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  // Only read the frontend directory. The Python .env and its keys stay outside Vite.
  const env = loadEnv(mode, process.cwd(), 'API_PROXY_');
  const target = env.API_PROXY_TARGET || 'http://127.0.0.1:8000';
  const proxy = { '/research': { target }, '/health': { target }, '/auth': { target }, '/account': { target }, '/admin': { target } };
  return {
    plugins: [react()],
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
    test: { environment: 'jsdom', setupFiles: './src/test/setup.ts', restoreMocks: true },
  };
});
