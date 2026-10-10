import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Keep development optimization out of the shared dependency junction.
export default defineConfig({plugins: [vue()], base: '/reader/', cacheDir: '../../.devtools/test-runs/frontend-gallery-entry-vite'})
