import { SharedReads } from './interaction.ts'
import { diagnostics, diagnosticId, errorType, mediaObservation, observeSignal, requestObservation, type RequestObservation } from './diagnostics.ts'

const reads = new SharedReads()
export interface ApiOptions extends RequestInit { readScope?: string }
export class ApiReadError extends Error {
  readonly code: string
  constructor(message: string, code: string) { super(message); this.code = code; if (code === 'read_timeout') this.name = 'TimeoutError' }
}
export function api<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const {readScope, ...requestOptions} = options
  if ((options.method || 'GET').toUpperCase() !== 'GET' || options.body) {
    const observation = requestObservation(path)
    observeSignal(options.signal, observation)
    return request<T>(path, requestOptions, observation)
  }
  const headers = [...new Headers(options.headers).entries()].sort(([a], [b]) => a.localeCompare(b))
  const key = JSON.stringify([path, headers, options.credentials, options.mode, options.cache,
    options.redirect, options.integrity, options.referrer, options.referrerPolicy, options.keepalive, readScope])
  return reads.read(key, (signal, observation) => request<T>(path, {...requestOptions, signal}, observation), options.signal, requestObservation(path))
}

async function request<T>(path: string, options: RequestInit, observation?: RequestObservation): Promise<T> {
  const signal = options.signal || new AbortController().signal
  const canRetry = (options.method || 'GET').toUpperCase() === 'GET'
  let busyRetried = false
  try {
    for (let attempt = 0; ; attempt++) {
      signal.throwIfAborted()
      const began = observation ? performance.now() : 0
      let outcome = 'error', reason = 'other', status: number | undefined, failureType: string | undefined
      let attemptObservation: RequestObservation | undefined
      let fetchOptions = {...options, signal}
      if (observation) {
        try {
          observation.request_id = diagnosticId(); observation.server_trace = undefined; observation.server_run = undefined
          attemptObservation = {...observation}
          const headers = new Headers(options.headers)
          headers.set('X-Pixiv-Client-Session', diagnostics.session)
          headers.set('X-Pixiv-Client-Request', observation.request_id)
          fetchOptions = {...fetchOptions, headers}
          const method = (options.method || 'GET').toUpperCase()
          diagnostics.emit('api.begin', {...attemptObservation, attempt, method: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'].includes(method) ? method : 'other', outcome: 'pending'})
        } catch { diagnostics.failure() }
      }
      const retry = (delay: number, cause: string) => {
        reason = cause
        diagnostics.emit('api.retry', {...attemptObservation, attempt, wait_ms: delay, reason: cause, status, outcome: 'pending'})
        return waitForRetry(delay, signal)
      }
      const parse = async (response: Response) => {
        const started = observation ? performance.now() : 0
        try {
          const value = await untilAborted(response.json(), signal, attemptObservation)
          diagnostics.emit('api.parsed', {...attemptObservation, attempt, outcome: 'ok', duration_ms: performance.now() - started})
          return value
        } catch (error) {
          reason = 'parse'
          diagnostics.emit('api.parsed', {...attemptObservation, attempt, outcome: signal.aborted ? 'cancelled' : 'error', duration_ms: performance.now() - started, error_type: errorType(error)})
          throw error
        }
      }
      try {
      let response: Response
      try {
        response = await untilAborted(fetch(path, fetchOptions), signal, attemptObservation)
      } catch (error) {
        signal.throwIfAborted()
        if (canRetry && attempt < 2 && error instanceof TypeError) {
          failureType = 'TypeError'
          await retry(attempt === 0 ? 350 : 1000, 'network')
          continue
        }
        throw error
      }
      status = response.status
      if (attemptObservation) {
        attemptObservation.server_trace = response.headers.get('X-Pixiv-Request-Id') || undefined
        attemptObservation.server_run = response.headers.get('X-Pixiv-Diagnostic-Run') || undefined
        Object.assign(observation!, attemptObservation)
      }
      diagnostics.emit('api.response', {...attemptObservation, attempt, status, duration_ms: performance.now() - began, outcome: response.ok ? 'ok' : 'error'})
      signal.throwIfAborted()
      let value
      let parsed = false
      // A rejected capacity admission can recover once; an exhausted SQL budget cannot.
      if (!response.ok && response.headers.get('Content-Type')?.includes('application/json')) {
        try { value = await parse(response); parsed = true }
        catch { signal.throwIfAborted() }
        const detail = value?.detail
        if (detail && typeof detail === 'object' && ['read_timeout', 'read_busy', 'read_cancelled'].includes(detail.code)) {
          if (detail.code === 'read_cancelled') throw new DOMException('读取已取消', 'AbortError')
          if (detail.code === 'read_busy' && canRetry && !busyRetried && attempt < 2) {
            busyRetried = true
            const header = response.headers.get('Retry-After')
            const delay = Math.min(3000, (header?.trim() ? retryDelay(header, 0) : 250) + Math.floor(Math.random() * 201))
            await retry(delay, 'busy')
            continue
          }
          throw new ApiReadError(typeof detail.message === 'string' && detail.message ? detail.message : httpError(response.status), detail.code)
        }
      }
      if (canRetry && attempt < 2 && [502, 503, 504].includes(response.status)) {
        const delay = retryDelay(response.headers.get('Retry-After'), attempt)
        try { if (response.body) await untilAborted(response.body.cancel(), signal) } catch { /* Failed body cleanup must not replace the HTTP failure. */ }
        signal.throwIfAborted()
        await retry(delay, 'http')
        continue
      }
      if (!parsed) {
        try {
          value = await parse(response)
        } catch (error) {
          signal.throwIfAborted()
          if (response.ok && error instanceof TypeError) throw error
          throw new Error(response.ok ? '服务返回的数据无法读取，请重试' : httpError(response.status))
        }
      }
      signal.throwIfAborted()
      if (!response.ok || value?.ok === false) {
        reason = 'http'
        if (value?.detail?.code === 'cursor_expired') throw new ApiReadError(value.detail.message || '图库已变化，请重新定位后继续', 'cursor_expired')
        const message = typeof value?.detail === 'string' ? value.detail : value?.detail?.message || value?.error
        throw new Error(typeof message === 'string' && message ? message : httpError(response.status))
      }
      outcome = 'ok'
      return value as T
      } catch (error) {
        failureType = errorType(error)
        if (signal.aborted) { outcome = 'cancelled'; reason = 'caller' }
        else if (failureType === 'TimeoutError') { outcome = 'timeout'; reason = 'timeout' }
        else if (failureType === 'AbortError') { outcome = 'cancelled'; reason = 'caller' }
        else if (failureType === 'TypeError' && reason !== 'parse') reason = 'network'
        throw error
      } finally {
        diagnostics.emit('api.end', {...attemptObservation, attempt, status, outcome, reason, error_type: failureType, aborted: signal.aborted, duration_ms: performance.now() - began})
      }
    }
  } catch (error) {
    if (signal.aborted) throw signal.reason
    if (error instanceof TypeError) throw new TypeError(canRetry ? '无法连接本地服务，请检查程序是否仍在运行' : '无法连接本地服务，操作结果尚未确认，请查询状态后再试')
    throw error
  }
}
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal, observation?: RequestObservation): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason) }
    signal.addEventListener('abort', abort, {once: true})
    promise.then(value => {
      if (signal.aborted && observation) diagnostics.emit('api.discarded', {...observation, outcome: 'discarded', reason: signal.reason instanceof Error && signal.reason.name === 'TimeoutError' ? 'timeout' : 'caller'})
      signal.removeEventListener('abort', abort); resolve(value)
    }, error => {
      signal.removeEventListener('abort', abort); reject(error)
    })
    if (signal.aborted) abort()
  })
}
function httpError(status: number): string {
  if (status === 503) return '服务暂不可用（503），请稍后重试'
  if (status === 502 || status === 504) return `服务响应暂时异常（${status}），请稍后重试`
  return `操作未完成（${status}），请重试`
}
function retryDelay(header: string | null, attempt: number): number {
  if (header !== null && header.trim()) {
    const seconds = Number(header)
    const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(header) - Date.now()
    if (Number.isFinite(delay)) return Math.min(3000, Math.max(0, delay))
  }
  return attempt === 0 ? 350 : 1000
}
function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted()
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, delay)
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(signal.reason) }
    signal.addEventListener('abort', abort, {once: true})
  })
}
export function write<T>(path: string, body: unknown, method = 'POST') {
  return api<T>(path, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
}
export function url(path: string, query: string, extra: Record<string, string | number> = {}) {
  const params = new URLSearchParams(query)
  for (const [key, value] of Object.entries(extra)) params.set(key, String(value))
  return path + '?' + params.toString()
}
const readyImages = new Map<string, HTMLImageElement>()
const imageFlights = new Map<string, {controller: AbortController; promise: Promise<HTMLImageElement>; users: number}>()
/** Readers and their adjacent prefetch prepare the final image, never a thumbnail. */
export async function preparePageImage(page: {src: string | null; preview?: string | null} | undefined, signal: AbortSignal): Promise<void> {
  if (!page) { signal.throwIfAborted(); return }
  await prepareImage(page.src, signal)
}
/** Share in-flight targets and retain at most eight ready adjacent images. */
export function prepareImage(src: string | null, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason)
  if (!src) return Promise.resolve()
  const ready = readyImages.get(src)
  if (ready) { readyImages.delete(src); readyImages.set(src, ready); return Promise.resolve() }
  let flight = imageFlights.get(src)
  if (!flight) {
    const controller = new AbortController()
    const promise = loadImage(src, controller.signal).then(image => {
      readyImages.set(src, image)
      while (readyImages.size > 8) readyImages.delete(readyImages.keys().next().value!)
      return image
    }).finally(() => { if (imageFlights.get(src)?.controller === controller) imageFlights.delete(src) })
    flight = {controller, promise, users: 0}; imageFlights.set(src, flight)
  }
  const owned = flight
  owned.users++
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (error?: unknown) => {
      if (settled) return
      settled = true; signal.removeEventListener('abort', abort)
      if (--owned.users === 0 && imageFlights.get(src) === owned) { imageFlights.delete(src); owned.controller.abort() }
      if (error) reject(error); else resolve()
    }
    const abort = () => finish(signal.reason)
    signal.addEventListener('abort', abort, {once: true})
    owned.promise.then(() => finish(), error => finish(error))
    if (signal.aborted) abort()
  })
}
function loadImage(src: string, signal: AbortSignal): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    const observation = mediaObservation(src)
    let settled = false
    const cleanup = () => { image.onload = null; image.onerror = null; signal.removeEventListener('abort', abort) }
    const finish = (failure?: Error) => {
      if (settled) return
      settled = true; cleanup()
      if (failure) {
        if (failure.name === 'AbortError') observation?.cancel('caller')
        else observation?.error(failure)
        image.removeAttribute('src'); reject(failure)
      }
      else resolve(image)
    }
    const abort = () => finish(new DOMException('Aborted', 'AbortError'))
    image.onload = () => {
      observation?.loaded(image)
      // load confirms display readiness. A separate decode promise must never
      // hold the old view indefinitely after the resource has loaded.
      observation?.decoded('unsupported')
      if (signal.aborted) { abort(); return }
      finish()
    }
    image.onerror = () => finish(new Error('目标图片未能载入，当前画面已保留，请重试'))
    signal.addEventListener('abort', abort, {once: true})
    if (signal.aborted) abort()
    else image.src = src
  })
}
