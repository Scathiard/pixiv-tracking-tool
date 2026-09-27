<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
const props = defineProps<{title: string; description: string; action: string; danger?: boolean}>()
const emit = defineEmits<{confirm: []; cancel: []}>()
const dialog = ref<HTMLElement | null>(null)
let previous: HTMLElement | null = null
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); emit('cancel') }
  if (event.key !== 'Tab') return
  const buttons = [...(dialog.value?.querySelectorAll<HTMLButtonElement>('button') || [])]
  const first = buttons[0], last = buttons.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(async () => { previous = document.activeElement as HTMLElement; document.querySelector('#app')?.setAttribute('inert', ''); await nextTick(); dialog.value?.querySelector('button')?.focus() })
onUnmounted(() => { document.querySelector('#app')?.removeAttribute('inert'); void nextTick(() => previous?.focus({preventScroll: true})) })
</script>
<template><Teleport to="body"><div class="confirm-overlay" @click.self="emit('cancel')"><section ref="dialog" class="confirm-dialog" role="alertdialog" aria-modal="true" :aria-label="props.title" @keydown="key"><h2>{{ title }}</h2><p>{{ description }}</p><div class="button-row"><button @click="emit('cancel')">取消</button><button :class="danger ? 'danger-button' : 'primary'" @click="emit('confirm')">{{ action }}</button></div></section></div></Teleport></template>
