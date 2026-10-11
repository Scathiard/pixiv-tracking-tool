import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Keep development optimization out of the shared dependency junction.
export default defineConfig({plugins: [vue()], base: '/reader/', cacheDir: process.env.PIXIV_FRONTEND_CACHE || '../../../tmp/frontend-gallery-entry-vite'})
