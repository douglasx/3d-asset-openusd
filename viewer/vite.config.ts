import { defineConfig } from 'vite';

// base './' so the built viewer works from any path: /viewer/ behind the API,
// a CDN prefix, or a static folder.
export default defineConfig({
  base: './',
  build: { target: 'es2022', chunkSizeWarningLimit: 1200 },
  server: { proxy: { '/assets': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000',
                     '/vessels': 'http://127.0.0.1:8000', '/files': 'http://127.0.0.1:8000' } },
});
