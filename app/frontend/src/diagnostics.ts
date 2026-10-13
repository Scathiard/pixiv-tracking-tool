import { requestId } from './state/requestId.ts'

export type DiagnosticCategory = 'gallery' | 'detail' | 'flow' | 'artists' | 'settings' | 'progress' | 'media' | 'bookmark' | 'task' | 'logs' | 'startup' | 'other'
type EventName = 'browser.start' | 'browser.navigation' | 'browser.visibility' | 'browser.error' | 'browser.unhandled' | 'browser.longtask' | 'browser.summary' | 'api.begin' | 'api.response' | 'api.parsed' | 'api.end' | 'api.retry' | 'api.discarded' | 'shared.join' | 'shared.leave' | 'shared.abandoned' | 'media.begin' | 'media.loaded' | 'media.decoded' | 'media.error' | 'media.cancel'
export type DiagnosticFields = Record<string, string | number | boolean | undefined>
export interface RequestObservation { operation_id: string; category: DiagnosticCategory; request_id?: string; server_trace?: string; server_run?: string }
const enums: Record<string, readonly string[]> = {
  category: ['gallery', 'detail', 'flow', 'artists', 'settings', 'progress', 'media', 'bookmark', 'task', 'logs', 'startup', 'other'],
  method: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'other'],
  outcome: ['ok', 'error', 'cancelled', 'timeout', 'pending', 'discarded', 'unsupported'],
  reason: ['caller', 'timeout', 'navigation', 'shared_empty', 'network', 'busy', 'http', 'parse', 'hidden', 'unmounted', 'other'],
  error_type: ['Error', 'TypeError', 'SyntaxError', 'RangeError', 'AbortError', 'TimeoutError', 'ApiReadError', 'Other'],
  error_source: ['js', 'vue', 'resource'],
}
const numbers = new Set(['duration_ms', 'wait_ms', 'count', 'attempt', 'status', 'bytes', 'width', 'height', 'consumer_count', 'media_ref'])
const booleans = new Set(['visible', 'aborted', 'shared'])
const ids: Record<string, number> = {request_id: 32, operation_id: 32, server_trace: 12, server_run: 12}
const names = new Set<EventName>(['browser.start', 'browser.navigation', 'browser.visibility', 'browser.error', 'browser.unhandled', 'browser.longtask', 'browser.summary', 'api.begin', 'api.response', 'api.parsed', 'api.end', 'api.retry', 'api.discarded', 'shared.join', 'shared.leave', 'shared.abandoned', 'media.begin', 'media.loaded', 'media.decoded', 'media.error', 'media.cancel'])

/** A strict local allowlist also protects against accidentally passing an Error or URL. */
export function safeFields(fields: DiagnosticFields): DiagnosticFields {
  const result: DiagnosticFields = {}
  for (const [key, value] of Object.entries(fields)) {
    if (typeof value === 'string' && (enums[key]?.includes(value) || (ids[key] && new RegExp(`^[0-9a-f]{${ids[key]}}$`).test(value)))) result[key] = value
    else if (typeof value === 'number' && numbers.has(key) && Number.isFinite(value) && value >= 0) result[key] = Math.min(value, Number.MAX_SAFE_INTEGER)
    else if (typeof value === 'boolean' && booleans.has(key)) result[key] = value
  }
  return result
}
export function category(path: string): DiagnosticCategory {
  const clean = path.split('?')[0] || ''
  if (/bookmark/.test(clean)) return 'bookmark'
  if (/media|thumbnail/.test(clean)) return 'media'
  if (/startup|upgrade/.test(clean)) return 'startup'
  if (/processing-log|logs/.test(clean)) return 'logs'
  if (/progress|status/.test(clean)) return 'progress'
  if (/tasks|operations/.test(clean)) return 'task'
  if (/settings|login|directories/.test(clean)) return 'settings'
  if (/artists|artist/.test(clean)) return 'artists'
  if (/reading-sessions|flow/.test(clean)) return 'flow'
  if (/works|illust/.test(clean)) return 'detail'
  if (/library|gallery|^\/$/.test(clean)) return 'gallery'
  return 'other'
}
export function errorType(error: unknown): string {
  try {
    if (error instanceof Error) return enums.error_type!.includes(error.name) ? error.name : 'Other'
  } catch { /* Never inspect error messages, stacks or arbitrary object properties. */ }
  return 'Other'
}
export function diagnosticId(): string { return requestId().replaceAll('-', '') }

/** One bounded queue and at most one independent, time-limited upload. No retries. */
export class BrowserDiagnostics {
  enabled = false
  session = ''
  run = ''
  private events: DiagnosticFields[] = []
  private dropped = 0
  private observerErrors = 0
  private sending = false
  private started = false
  private probing = false
  private timer: ReturnType<typeof setInterval> | undefined
  get observing(): boolean { return this.enabled || this.probing }
  async start(): Promise<void> {
    if (this.started) return
    this.started = true
    try {
      this.session = diagnosticId(); this.probing = true
      this.emit('browser.start', {category: currentCategory(), visible: typeof document !== 'undefined' && !document.hidden})
      const response = await fetch('/api/diagnostics', {cache: 'no-store', signal: AbortSignal.timeout(5000)})
      if (!response.ok) return
      const info = await response.json()
      if (info?.enabled !== true || typeof info.run_id !== 'string' || !/^[0-9a-f]{12}$/.test(info.run_id)) return
      this.run = info.run_id; this.enabled = true
      this.timer = setInterval(() => { void this.flush() }, 2000)
    } catch { this.observerErrors++ }
    finally {
      this.probing = false
      if (!this.enabled) { this.events = []; this.dropped = 0; this.observerErrors = 0 }
    }
  }
  emit(event: EventName, fields: DiagnosticFields = {}): void {
    if (!this.observing) return
    try {
      if (!names.has(event)) { this.observerErrors++; return }
      if (this.events.length >= 256) { this.dropped++; return }
      this.events.push({...safeFields(fields), event, at_ms: performance.now(), utc_ms: Date.now()})
    } catch { this.observerErrors++ }
  }
  failure(): void { if (this.observing) this.observerErrors++ }
  async flush(keepalive = false): Promise<void> {
    if (!this.enabled || this.sending || (!this.events.length && !this.dropped && !this.observerErrors)) return
    this.sending = true
    const events = this.events.splice(0, 32), dropped = this.dropped, observer_errors = this.observerErrors
    try {
      const body = JSON.stringify({session: this.session, events, dropped, observer_errors})
      if (new TextEncoder().encode(body).byteLength > 32768) throw new Error()
      const response = await fetch('/api/diagnostics/events', {method: 'POST', headers: {'Content-Type': 'application/json'}, body, keepalive, signal: AbortSignal.timeout(5000)})
      if (response.status !== 204) throw new Error()
      this.dropped -= dropped; this.observerErrors -= observer_errors
    } catch { this.dropped += events.length; this.observerErrors++ }
    finally { this.sending = false }
  }
  stop(): void { clearInterval(this.timer); this.enabled = false; this.probing = false; this.events = [] }
}
export const diagnostics = new BrowserDiagnostics()
const signalObservations = new WeakMap<AbortSignal, RequestObservation[]>()
export function observeSignal(signal: AbortSignal | null | undefined, observation?: RequestObservation): void {
  if (!signal || !observation) return
  const observations = signalObservations.get(signal) || []
  // A ticket can cover several requests; bound the diagnostic references too.
  if (observations.length < 32) observations.push(observation)
  else diagnostics.failure()
  signalObservations.set(signal, observations)
}
export function discardedSignal(signal: AbortSignal, reason: 'navigation' | 'other' = 'navigation'): void {
  for (const observation of signalObservations.get(signal) || []) diagnostics.emit('api.discarded', {...observation, outcome: 'discarded', reason})
  signalObservations.delete(signal)
}
export function requestObservation(path: string): RequestObservation | undefined {
  if (!diagnostics.observing) return
  try { return {operation_id: diagnosticId(), category: category(path)} }
  catch { diagnostics.failure(); return }
}
export function browserError(error: unknown, vue = false): void {
  diagnostics.emit('browser.error', {error_type: errorType(error), error_source: vue ? 'vue' : 'js', category: currentCategory(), outcome: 'error'})
}
export function currentCategory(): DiagnosticCategory { return typeof location !== 'undefined' ? category(location.pathname) : 'other' }
export function installBrowserObservers(): (() => void) | undefined {
  if (!diagnostics.observing) return
  const error = (event: Event) => {
    if (event instanceof ErrorEvent) browserError(event.error)
    else if (event.target instanceof HTMLImageElement) diagnostics.emit('browser.error', {error_source: 'resource', error_type: 'Other', category: 'media', outcome: 'error'})
  }
  const unhandled = (event: PromiseRejectionEvent) => diagnostics.emit('browser.unhandled', {error_type: errorType(event.reason), error_source: 'js', outcome: 'error', category: currentCategory()})
  const visibility = () => { diagnostics.emit('browser.visibility', {visible: !document.hidden, category: currentCategory()}); if (document.hidden) void diagnostics.flush(true) }
  const pagehide = () => { diagnostics.emit('browser.visibility', {visible: false, reason: 'navigation'}); void diagnostics.flush(true) }
  window.addEventListener('error', error, true)
  window.addEventListener('unhandledrejection', unhandled)
  document.addEventListener('visibilitychange', visibility)
  window.addEventListener('pagehide', pagehide)
  let count = 0, duration = 0, longest = 0
  let observer: PerformanceObserver | undefined
  try {
    if (!PerformanceObserver.supportedEntryTypes.includes('longtask')) diagnostics.emit('browser.longtask', {outcome: 'unsupported'})
    else {
      observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) { count++; duration += entry.duration; longest = Math.max(longest, entry.duration) }
      })
      observer.observe({type: 'longtask', buffered: false})
    }
  } catch { diagnostics.failure(); diagnostics.emit('browser.longtask', {outcome: 'unsupported'}) }
  const timer = setInterval(() => {
    if (count) diagnostics.emit('browser.longtask', {count, duration_ms: duration, wait_ms: longest})
    diagnostics.emit('browser.summary', {visible: !document.hidden})
    count = 0; duration = 0; longest = 0
  }, 30000)
  return () => {
    observer?.disconnect(); clearInterval(timer)
    window.removeEventListener('error', error, true); window.removeEventListener('unhandledrejection', unhandled)
    document.removeEventListener('visibilitychange', visibility); window.removeEventListener('pagehide', pagehide)
  }
}

let mediaSerial = 0
/** Native load confirms load; decode timing is unavailable unless the caller already decodes. */
export function mediaObservation(src: string | null | undefined) {
  if (!src || !diagnostics.observing) return undefined
  const media_ref = ++mediaSerial, began = performance.now()
  let ended = false
  const fields = (): DiagnosticFields => {
    try {
      const entries = performance.getEntriesByName(new URL(src, location.href).href, 'resource') as PerformanceResourceTiming[]
      // Do not attach an older load's trace when this load has no visible timing entry.
      let trace: string | undefined
      for (let index = entries.length - 1; index >= 0; index--) {
        const entry = entries[index]!
        if (entry.startTime >= began) { trace = entry.serverTiming?.find(timing => timing.name === 'pixiv')?.description; break }
      }
      return {media_ref, category: 'media', server_trace: trace, duration_ms: performance.now() - began}
    } catch { diagnostics.failure(); return {media_ref, category: 'media', duration_ms: performance.now() - began} }
  }
  diagnostics.emit('media.begin', {media_ref, category: 'media', outcome: 'pending'})
  return {
    loaded(image: HTMLImageElement) { if (!ended) diagnostics.emit('media.loaded', {...fields(), outcome: 'ok', width: image.naturalWidth, height: image.naturalHeight}) },
    decoded(outcome: 'ok' | 'error' | 'unsupported', error?: unknown) { if (!ended) { diagnostics.emit('media.decoded', {...fields(), outcome, error_type: error === undefined ? undefined : errorType(error)}); ended = true } },
    error(error?: unknown) { if (!ended) { diagnostics.emit('media.error', {...fields(), error_source: 'resource', outcome: 'error', error_type: error === undefined ? undefined : errorType(error)}); ended = true } },
    cancel(reason: 'hidden' | 'unmounted' | 'navigation' | 'timeout' | 'caller' = 'navigation') { if (!ended) { diagnostics.emit('media.cancel', {...fields(), outcome: reason === 'timeout' ? 'timeout' : 'cancelled', reason}); ended = true } },
  }
}
