import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({
  plugins: [vue()], base: '/reader/',
  build: { emptyOutDir: false, sourcemap: false },
})
