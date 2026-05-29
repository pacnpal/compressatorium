import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import path from 'node:path';

export default defineConfig(({ command }) => ({
  plugins: [svelte()],
  resolve: {
    alias: { $lib: path.resolve('./src/lib') },
  },
  base: command === 'build' ? '/static/' : '/',
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true, ws: false },
      '/health': 'http://localhost:8080',
    },
  },
  build: {
    outDir: 'static',
    emptyOutDir: false,
    assetsDir: 'assets',
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
}));
