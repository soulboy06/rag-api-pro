import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Use the same host name as the Docker-published API. On Windows, 127.0.0.1
// and localhost may resolve to different listeners when an old local API is
// still running, which can make the UI read stale document metadata.
const backendTarget = process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: backendTarget,
        changeOrigin: true,
      },
      '/health': {
        target: backendTarget,
        changeOrigin: true,
      }
    }
  }
});
