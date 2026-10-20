<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import type {Anchor, PageData, PageLoader} from './types'
import {preparePageImage} from './api'
import {LatestIntent} from './interaction'
import {StreamWheelDrag, STREAM_WHEEL_QUIET, STREAM_WHEEL_DISTANCE, STREAM_PULL_DISTANCE, streamCanCommit, streamDragOffset, streamIdentity, sameStreamWork, type StreamInitialOpening, type StreamNeighborLoader, type StreamCandidate} from './state/streamReader'
import StreamWorkPanel from './components/StreamWorkPanel.vue'

const props = defineProps<{
  loader: PageLoader; createPreloadLoader?: () => PageLoader; prepareNeighbor?: StreamNeighborLoader; commitSource?: () => void
  preloadPaused?: boolean; initialOpening?: StreamInitialOpening | null; initial: Anchor | null; sessionKey: string; inactive?: boolean
}>()
const emit = defineEmits<{
  focus: [page: PageData, anchor: Anchor]; navigate: [delta: number]
  activity: [state: {phase: string; error: string}]; recover: [page: PageData | null]
}>()
interface Slot extends StreamCandidate {
  key: string; position: number; anchor: Anchor; ready: boolean; phase: string; error: string
}
const root = ref<HTMLDivElement | null>(null), slots = ref<Slot[]>([]), activeKey = ref('')
const displacement = ref(0), settling = ref(false), booting = ref(false), notice = ref(''), navigationError = ref('')
const initialIntent = new LatestIntent(), wheelGesture = new StreamWheelDrag()
const panels = new Map<string, InstanceType<typeof StreamWorkPanel>>()
const candidates = new Map<number, {owner: string; controller: AbortController}>()
const neighborStatus = ref<Record<number, {phase: string; error: string; absent: boolean}>>({})
let sequence = 0, mounted = false, observer: ResizeObserver | null = null, height = 1
let settleTimer = 0, wheelTimer = 0, preloadTimer = 0, motion = 0, entry = 0
let completeAnimation: ((value: boolean) => void) | null = null, lastDirection = 1
let touch: {id: number; x: number; y: number; delta: number; top: boolean; bottom: boolean; blocked: boolean} | null = null
const active = computed(() => slots.value.find(slot => slot.key === activeKey.value))
const activity = computed(() => ({
  phase: booting.value ? '正在准备原图，当前画面会保留…' : notice.value || active.value?.phase || '',
  error: navigationError.value || active.value?.error || '',
}))
watch(activity, value => emit('activity', value), {deep: true})
const paint = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
function panel() { return panels.get(activeKey.value) }
function currentPage() { return panel()?.snapshot()?.page || active.value?.target || null }
function slotName(position: number) { return position < 0 ? 'previous' : position > 0 ? 'next' : 'current' }
function anchorFor(page: PageData): Anchor {
  return {work_id: page.work_id, work_index: page.work_index, index: page.index, page: page.page, offset: 0}
}
function makeSlot(candidate: StreamCandidate, position: number, anchor = anchorFor(candidate.target)): Slot {
  return {...candidate, key: props.sessionKey + ':' + streamIdentity(candidate.target) + ':' + ++sequence,
    position, anchor, ready: false, phase: '', error: ''}
}
function stopCandidates() {
  clearTimeout(preloadTimer); preloadTimer = 0
  for (const candidate of candidates.values()) candidate.controller.abort()
  candidates.clear()
}
function stopMotion() {
  ++motion; clearTimeout(settleTimer); clearTimeout(wheelTimer); settling.value = false
  completeAnimation?.(false); completeAnimation = null; touch = null; displacement.value = 0
  wheelGesture.block(performance.now())
}
async function animate(to: number): Promise<boolean> {
  const serial = ++motion
  settling.value = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
  await nextTick()
  if (serial !== motion) return false
  displacement.value = to
  if (!settling.value) { await paint(); return serial === motion }
  return new Promise(resolve => {
    completeAnimation = resolve
    settleTimer = window.setTimeout(() => {
      completeAnimation = null; settling.value = false; resolve(serial === motion)
    }, 230)
  })
}
function focused(slot: Slot, page: PageData, anchor: Anchor) {
  slot.target = page
  if (slot.key !== activeKey.value || booting.value) return
  slot.anchor = anchor; emit('focus', page, anchor); queueNeighbors()
}
function panelReady(slot: Slot) {
  slot.ready = true
  if (slot.key === activeKey.value) { booting.value = false; queueNeighbors() }
  else if (notice.value && !navigationError.value) notice.value = ''
}
function panelActivity(slot: Slot, state: {phase: string; error: string}) {
  slot.phase = state.phase; slot.error = state.error
  if (state.error) slot.ready = false
}
function fallbackNeighbor(source: PageLoader): StreamNeighborLoader {
  const prepare: StreamNeighborLoader = async (page, direction, signal) => {
    const index = direction > 0 ? page.has_next_work === false ? null : page.next_work_index : page.previous_work_index
    if (index == null || index < 0 || index === page.index - page.page) return null
    const loader = props.createPreloadLoader?.() || source
    let batch = await loader(index, 4, signal)
    let target = batch.items.find(item => item.index === index)
    if (!target) return null
    if (direction < 0) {
      const end = target.index - target.page + target.page_count - 1
      batch = await loader(Math.max(index, end - 3), Math.min(4, target.page_count), signal)
      target = batch.items.find(item => item.index === end)
      if (!target) return null
    }
    return {batch, target, loader, prepareNeighbor: prepare, commit: () => {}, tail: direction < 0}
  }
  return prepare
}
async function prepare(direction: number) {
  const owner = active.value, page = currentPage()
  if (!owner || !page || props.inactive || props.preloadPaused || document.hidden) return
  if (slots.value.some(slot => slot.position === direction) || candidates.get(direction)?.owner === owner.key || neighborStatus.value[direction]?.absent) return
  const controller = new AbortController()
  const request = {owner: owner.key, controller}; candidates.set(direction, request)
  neighborStatus.value[direction] = {phase: '正在准备相邻作品…', error: '', absent: false}
  const owns = () => !controller.signal.aborted && activeKey.value === owner.key && candidates.get(direction) === request
  try {
    const candidate = await owner.prepareNeighbor(page, direction, controller.signal)
    if (!owns()) return
    if (!candidate) { neighborStatus.value[direction] = {phase: '', error: '', absent: true}; return }
    neighborStatus.value[direction]!.phase = '正在准备相邻原图…'
    if (!candidate.target.src) throw new Error('相邻作品原图尚未就绪，当前画面已保留，请稍后重试')
    await preparePageImage(candidate.target, controller.signal)
    if (!owns()) return
    const target = candidate.target
    candidate.batch = {...candidate.batch, items: candidate.batch.items.filter(item => sameStreamWork(item, target))}
    slots.value = [...slots.value.filter(slot => slot.position !== direction), makeSlot(candidate, direction)]
  } catch (failure) {
    if (owns()) neighborStatus.value[direction] = {phase: '', error: failure instanceof Error ? failure.message : '相邻作品暂时无法读取', absent: false}
  } finally { if (candidates.get(direction) === request) candidates.delete(direction) }
}
function queueNeighbors() {
  if (!active.value?.ready || props.inactive || props.preloadPaused || document.hidden || settling.value) return
  if (preloadTimer) return
  preloadTimer = window.setTimeout(() => { preloadTimer = 0; void prepare(-1); void prepare(1) }, 100)
}
function rememberBoundary() {
  const owner = active.value, selected = panel()?.snapshot()
  if (owner && selected) { owner.anchor = selected.anchor; owner.target = selected.page }
}
function pendingMessage(direction: number) {
  const slot = slots.value.find(slot => slot.position === direction)
  const state = neighborStatus.value[direction]
  if (slot?.error || state?.error) return slot?.error || state?.error || ''
  if (state?.absent) return direction > 0 ? '已到当前范围末尾' : '已到当前范围开头'
  return slot ? '正在准备相邻画面，请稍后再滑动' : state?.phase || '正在准备相邻作品，请稍后再滑动'
}
async function release(delta: number, cancelled = false, threshold = STREAM_PULL_DISTANCE) {
  if (settling.value || booting.value) return
  touch = null; clearTimeout(wheelTimer)
  // Native scrolling never moved the track. Do not lock the next edge gesture
  // behind an animation from zero to zero; keyboard commits still animate.
  if (!displacement.value && (cancelled || Math.abs(delta) < threshold)) { queueNeighbors(); return }
  const direction = delta < 0 ? 1 : -1
  lastDirection = direction
  const incoming = slots.value.find(slot => slot.position === direction)
  const crossed = Math.abs(delta) >= threshold
  if (!streamCanCommit(delta, !!incoming?.ready && !incoming.error, cancelled, threshold) || props.inactive || props.preloadPaused) {
    if (crossed && !cancelled && !props.inactive) {
      const text = pendingMessage(direction)
      if (incoming?.error || neighborStatus.value[direction]?.error) navigationError.value = text
      else notice.value = text
      void prepare(direction)
    }
    if (displacement.value) await animate(0)
    queueNeighbors(); return
  }
  const previous = active.value
  if (!previous || !incoming) return
  rememberBoundary()
  const completed = await animate(-direction * height)
  if (!completed || props.inactive || props.preloadPaused || activeKey.value !== previous.key) { displacement.value = 0; return }
  stopCandidates()
  // The arriving panel is kept mounted. Its viewport and image nodes become current in place.
  incoming.commit()
  settling.value = false
  activeKey.value = incoming.key
  slots.value = slots.value.filter(slot => slot.key === incoming.key || slot.key === previous.key)
  previous.position = -direction; incoming.position = 0
  displacement.value = 0; neighborStatus.value = {}; notice.value = ''; navigationError.value = ''; entry++
  await nextTick()
  // A backward visit may have left the next work in its middle via a keyboard command.
  // Reposition that offscreen viewport; the arriving panel itself remains untouched.
  if (direction < 0 && (previous.anchor.page !== 0 || previous.anchor.offset > 0)) {
    previous.ready = false
    void panels.get(previous.key)?.firstScreen()
  }
  const selected = panels.get(incoming.key)?.snapshot()
  if (selected) focused(incoming, selected.page, selected.anchor)
  wheelGesture.block(performance.now()); queueNeighbors()
}
function blocked(target: EventTarget | null) {
  return props.inactive || props.preloadPaused || booting.value || settling.value || !!active.value?.phase || !!(target instanceof Element &&
    target.closest('button,a,input,select,textarea,[contenteditable],[data-no-navigation],[data-inline-zoom-active="true"],[data-inline-zoom-consumed="true"]')) ||
    (window.visualViewport?.scale || 1) > 1.01
}
function atEdge(direction: number) { return panel()?.atEdge(direction) || false }
function touchStart(event: TouchEvent) {
  if (event.touches.length !== 1) { if (touch) touch.blocked = true; displacement.value = 0; return }
  const point = event.touches[0]!
  touch = {id: point.identifier, x: point.clientX, y: point.clientY, delta: 0, top: atEdge(-1), bottom: atEdge(1),
    blocked: blocked(event.target) || event.defaultPrevented}
}
function touchMove(event: TouchEvent) {
  if (!touch || touch.blocked) return
  if (event.touches.length !== 1 || blocked(event.target) || event.defaultPrevented) {
    touch.blocked = true; displacement.value = 0; return
  }
  const point = [...event.touches].find(point => point.identifier === touch!.id)
  if (!point) return
  const delta = point.clientY - touch.y, horizontal = Math.abs(point.clientX - touch.x)
  if (horizontal > Math.abs(delta) * 1.3 && horizontal > 18) { touch.blocked = true; displacement.value = 0; return }
  const direction = delta > 0 ? -1 : 1
  if (!(direction < 0 ? touch.top : touch.bottom) || !atEdge(direction)) { displacement.value = 0; touch.delta = 0; return }
  if (event.cancelable) event.preventDefault()
  touch.delta = delta; displacement.value = streamDragOffset(delta, height)
}
function touchEnd(event: TouchEvent) {
  if (event.touches.length) return
  const selected = touch; touch = null
  if (!selected) return
  void release(selected.delta, selected.blocked || event.defaultPrevented || event.type === 'touchcancel' || blocked(event.target))
}
function wheel(event: WheelEvent) {
  const now = performance.now()
  if (blocked(event.target) || event.defaultPrevented || event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
    wheelGesture.block(now); return
  }
  const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1)
  const drag = wheelGesture.feed(delta, now, atEdge)
  if (atEdge(Math.sign(delta)) && event.cancelable) event.preventDefault()
  if (!drag) return
  displacement.value = streamDragOffset(drag, height)
  clearTimeout(wheelTimer)
  wheelTimer = window.setTimeout(() => { void release(drag, false, STREAM_WHEEL_DISTANCE) }, STREAM_WHEEL_QUIET)
}
function zoomOwned() { touch = null; displacement.value = 0; clearTimeout(wheelTimer); wheelGesture.block(performance.now()) }
function navigateWork(direction: number) {
  if (props.inactive || props.preloadPaused || booting.value || settling.value || !active.value) return
  direction = Math.sign(direction)
  if (!direction) return
  lastDirection = direction
  if (!slots.value.some(slot => slot.position === direction && slot.ready && !slot.error)) {
    const text = pendingMessage(direction)
    if (neighborStatus.value[direction]?.error) navigationError.value = text
    else notice.value = text
    void prepare(direction); return
  }
  void release(-direction * Math.max(STREAM_PULL_DISTANCE, height * .25))
}
async function boot() {
  stopMotion(); stopCandidates(); neighborStatus.value = {}; navigationError.value = ''; notice.value = ''
  const request = initialIntent.begin(), session = props.sessionKey, source = props.loader
  booting.value = true
  try {
    const index = props.initial?.index || 0
    const opening = props.initialOpening?.sessionKey === session ? props.initialOpening.batch : null
    const batch = opening && opening.items.some(page => page.index === index) ? opening : await source(Math.max(0, index - 16), 64, request.signal)
    request.signal.throwIfAborted()
    const target = batch.items.find(page => page.index === index)
    if (!target) {
      if (batch.total) throw new Error('目标作品已不在当前范围内，请重试')
      slots.value = []; activeKey.value = ''; booting.value = false; return
    }
    await preparePageImage(target, request.signal)
    if (!request.current() || session !== props.sessionKey) return
    const candidate: StreamCandidate = {batch, target, loader: source, prepareNeighbor: props.prepareNeighbor || fallbackNeighbor(source),
      commit: props.commitSource || (() => {})}
    const anchor = {...anchorFor(target), offset: props.initial?.offset || 0}
    const previous = active.value
    if (previous && previous.target.work_id === target.work_id && previous.target.revision_id === target.revision_id) {
      // Order changes can rebase ordinals while the current image remains the same.
      slots.value = [previous]
      Object.assign(previous, candidate, {position: 0, anchor})
      await nextTick(); await panels.get(previous.key)?.rebase(batch, anchor)
      booting.value = false
      const selected = panels.get(previous.key)?.snapshot()
      if (selected) focused(previous, selected.page, selected.anchor)
    } else {
      const slot = makeSlot(candidate, 0, anchor)
      slots.value = [slot]; activeKey.value = slot.key
    }
  } catch (failure) {
    if (request.current()) { booting.value = false; navigationError.value = failure instanceof Error ? failure.message : '作品暂时无法载入' }
  }
}
function jump(index: number, offset = 0) {
  const page = currentPage()
  if (page && index >= page.index - page.page && index < page.index - page.page + page.page_count) return panel()?.jump(index, offset)
  if (page) navigateWork(index < page.index ? -1 : 1)
}
function cancel() {
  initialIntent.cancel(); stopMotion(); stopCandidates(); booting.value = false; notice.value = ''; navigationError.value = ''
  panel()?.cancel()
}
function retry() {
  navigationError.value = ''; notice.value = ''
  if (!active.value) { void boot(); return }
  if (active.value.error) { panel()?.retry(); return }
  const failed = slots.value.find(slot => slot.position === lastDirection && slot.error)
  if (failed) { panels.get(failed.key)?.retry(); return }
  delete neighborStatus.value[lastDirection]; void prepare(lastDirection)
}
function visibilityChanged() {
  if (document.hidden) { stopMotion(); stopCandidates() }
  else queueNeighbors()
}
function archiveUpdated() {
  stopMotion(); stopCandidates(); slots.value = slots.value.filter(slot => slot.key === activeKey.value); neighborStatus.value = {}; queueNeighbors()
}
watch(() => props.sessionKey, () => { if (mounted) void boot() })
watch(() => props.inactive, inactive => { if (inactive) { stopMotion(); stopCandidates() } else queueNeighbors() })
watch(() => props.preloadPaused, paused => { if (paused) { stopMotion(); stopCandidates() } else queueNeighbors() })
onMounted(() => {
  mounted = true
  observer = new ResizeObserver(entries => {
    const nextHeight = entries[0]?.contentRect.height || 1
    if (Math.abs(height - nextHeight) > 1) stopMotion()
    height = nextHeight
  })
  if (root.value) { height = root.value.clientHeight || 1; observer.observe(root.value) }
  document.addEventListener('visibilitychange', visibilityChanged); window.addEventListener('archive-updated', archiveUpdated)
  void boot()
})
onBeforeUnmount(() => {
  mounted = false; initialIntent.cancel(); stopMotion(); stopCandidates(); observer?.disconnect()
  document.removeEventListener('visibilitychange', visibilityChanged); window.removeEventListener('archive-updated', archiveUpdated)
})
defineExpose({readAnchor: () => panel()?.readAnchor() || null, jump, navigateWork, stepPage: (direction: number) => panel()?.stepPage(direction),
  cancel, retry, scrollBy: (delta: number) => panel()?.scrollBy(delta)})
</script>
<template>
  <div ref="root" class="stream-reader" :data-stream-work="active?.target.work_id" :data-stream-entry="entry"
    :data-track-offset="displacement" :data-track-settling="String(settling)"
    @touchstart="touchStart" @touchmove="touchMove" @touchend="touchEnd" @touchcancel="touchEnd"
    @wheel="wheel" @inline-zoom-gesture="zoomOwned">
    <div class="stream-track" :class="{'is-settling': settling}" :style="{transform: 'translateY(' + displacement + 'px)'}">
      <div v-for="slot in slots" :key="slot.key" class="stream-slot" :data-stream-slot="slotName(slot.position)"
        :data-work-id="slot.target.work_id" :data-round="slot.target.round || 0" :data-panel-ready="String(slot.ready)"
        :aria-hidden="slot.position !== 0 ? true : undefined" :inert="slot.position !== 0 ? true : undefined"
        :style="{transform: 'translateY(' + slot.position * 100 + '%)'}">
        <StreamWorkPanel :ref="element => { if (element) panels.set(slot.key, element as InstanceType<typeof StreamWorkPanel>); else panels.delete(slot.key) }"
          :loader="slot.loader" :opening="slot.batch" :initial="slot.anchor" :tail="slot.tail" :active="slot.key === activeKey"
          :inactive="inactive || settling || booting" @ready="panelReady(slot)" @focus="(page, anchor) => focused(slot, page, anchor)"
          @activity="state => panelActivity(slot, state)" @navigate="navigateWork" @recover="emit('recover', $event)" />
      </div>
      <template v-for="direction in [-1, 1]" :key="direction">
        <div v-if="!slots.some(slot => slot.position === direction)" class="stream-neighbor-placeholder"
          :data-stream-slot="slotName(direction)" :style="{transform: 'translateY(' + direction * 100 + '%)'}" aria-hidden="true">
          {{ pendingMessage(direction) }}
        </div>
      </template>
    </div>
    <p v-if="!booting && !slots.length" class="stream-empty">当前范围没有可阅读的作品。</p>
    <div v-if="displacement" class="stream-pull" :class="{'stream-pull-bottom': displacement < 0}" aria-hidden="true">
      {{ slots.some(slot => slot.position === (displacement < 0 ? 1 : -1) && slot.ready) ? Math.abs(displacement) >= STREAM_PULL_DISTANCE ? '松手切换作品' : '继续滑动切换作品' : pendingMessage(displacement < 0 ? 1 : -1) }}
    </div>
  </div>
</template>
<style scoped>
.stream-reader { position: absolute; inset: 0; min-width: 0; overflow: hidden; touch-action: pan-y pinch-zoom; }
.stream-track { position: absolute; inset: 0; will-change: transform; }
.stream-track.is-settling { transition: transform 220ms cubic-bezier(.22,.7,.2,1); }
.stream-slot, .stream-neighbor-placeholder { position: absolute; inset: 0; overflow: hidden; background: var(--art-bg); }
.stream-neighbor-placeholder { display: grid; place-content: center; padding: 24px; text-align: center; color: var(--muted); }
.stream-empty { padding: 32px 16px; text-align: center; color: var(--muted); }
.stream-pull { position: absolute; left: 50%; top: 12px; transform: translateX(-50%); padding: 6px 12px; border-radius: 20px; background: var(--surface); color: var(--muted); font-size: 12px; pointer-events: none; white-space: nowrap; }
.stream-pull-bottom { top: auto; bottom: 12px; }
@media(prefers-reduced-motion:reduce) { .stream-track.is-settling { transition: none; } }
</style>
