<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{value?: number | null}>()
const label = computed(() => props.value === 2 ? 'R-18G' : props.value === 1 ? 'R-18' : '')
</script>
<template><span v-if="label" class="restriction-badge">{{ label }}</span></template>
