import { defineConfig } from 'astro/config';

export default defineConfig({
  output: 'static',
  vite: {
    build: {
      cssCodeSplit: true,
    },
  },
  compressHTML: true,
  prefetch: true,
});