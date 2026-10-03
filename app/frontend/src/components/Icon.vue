<script setup lang="ts">
defineProps<{name: 'grid' | 'book' | 'flow' | 'artists' | 'settings' | 'heart' | 'search' | 'filter' | 'left' | 'right' | 'down' | 'close' | 'plus' | 'folder' | 'external' | 'refresh' | 'check' | 'pause' | 'play' | 'list' | 'zoom' | 'menu' | 'clock'}>()
const paths: Record<string, string> = {
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  book: 'M4 4.5A2.5 2.5 0 0 1 6.5 2H20v18H6.5A2.5 2.5 0 0 1 4 17.5zM4 17.5A2.5 2.5 0 0 1 6.5 15H20',
  flow: 'M5 3v18M5 6h13l-3-3m3 3-3 3M10 13h9l-3-3m3 3-3 3M10 20h9l-3-3m3 3-3 3',
  artists: 'M16 20H3v-1a6.5 6.5 0 0 1 13 0v1zM9.5 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM19 8a3 3 0 0 1 0 6m1 6h1v-1a5 5 0 0 0-3-4.6',
  settings: 'M9 5.57L9.86 2.74L14.14 2.74L15 5.57L16.07 6.18L18.95 5.52L21.08 9.22L19.07 11.38L19.07 12.62L21.08 14.78L18.95 18.48L16.07 17.82L15 18.43L14.14 21.26L9.86 21.26L9 18.43L7.93 17.82L5.05 18.48L2.92 14.78L4.93 12.62L4.93 11.38L2.92 9.22L5.05 5.52L7.93 6.18ZM12 8.8a3.2 3.2 0 1 0 0 6.4 3.2 3.2 0 0 0 0-6.4Z',
  heart: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z',
  search: 'M10.8 18.1a7.3 7.3 0 1 0 0-14.6 7.3 7.3 0 0 0 0 14.6zM16.2 16.2 21 21',
  filter: 'M3 6h18M7 12h14M3 18h18M7 4v4M17 10v4M9 16v4',
  left: 'm15 4-8 8 8 8', right: 'm9 4 8 8-8 8', down: 'm4 9 8 8 8-8', close: 'M5 5l14 14M19 5 5 19', plus: 'M12 4v16M4 12h16',
  folder: 'M3 6h7l2 2h9v12H3z', external: 'M13 4h7v7M20 4l-9 9M18 14v6H4V6h6',
  refresh: 'M20 6v5h-5M4 18v-5h5M5.2 9a7 7 0 0 1 12.5-2L20 11M4 13l2.3 4a7 7 0 0 0 12.5-2',
  check: 'm4 12 5 5L20 6', pause: 'M7 4v16M17 4v16', play: 'm8 4 12 8-12 8z', list: 'M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1',
  zoom: 'M10.8 18.1a7.3 7.3 0 1 0 0-14.6 7.3 7.3 0 0 0 0 14.6zM16.2 16.2 21 21M10.8 7v7M7.3 10.5h7',
  menu: 'M4 6h16M4 12h16M4 18h16', clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2',
}
</script>
<template><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="icon"><path :d="paths[name]" /></svg></template>
