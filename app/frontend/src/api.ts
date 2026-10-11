import { SharedReads } from './interaction.ts'

const reads = new SharedReads()
export interface ApiOptions extends RequestInit { readScope?: string }
export class ApiReadError extends Error {
  readonly code: string
  constructor(message: string, code: string) { super(message); this.code = code; if (code === 'read_timeout') this.name = 'TimeoutError' }
}
export function api<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const {readScope, ...requestOptions} = options
  if ((options.method || 'GET').toUpperCase() !== 'GET' || options.body) return request<T>(path, requestOptions)
  const headers = [...new Headers(options.headers).entries()].sort(([a], [b]) => a.localeCompare(b))
  const key = JSON.stringify([path, headers, options.credentials, options.mode, options.cache,
    options.redirect, options.integrity, options.referrer, options.referrerPolicy, options.keepalive, readScope])
  return reads.read(key, signal => request<T>(path, {...requestOptions, signal}), options.signal)
}

async function request<T>(path: string, options: RequestInit): Promise<T> {
  const signal = options.signal
    ? AbortSignal.any([options.signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000)
  const canRetry = (options.method || 'GET').toUpperCase() === 'GET'
  let busyRetried = false
  try {
    for (let attempt = 0; ; attempt++) {
      signal.throwIfAborted()
      let response: Response
      try {
        response = await untilAborted(fetch(path, {...options, signal}), signal)
      } catch (error) {
        signal.throwIfAborted()
        if (canRetry && attempt < 2 && error instanceof TypeError) {
          await waitForRetry(attempt === 0 ? 350 : 1000, signal)
          continue
        }
        throw error
      }
      signal.throwIfAborted()
      let value
      let parsed = false
      // A rejected capacity admission can recover once; an exhausted SQL budget cannot.
      if (!response.ok && response.headers.get('Content-Type')?.includes('application/json')) {
        try { value = await untilAborted(response.json(), signal); parsed = true }
        catch { signal.throwIfAborted() }
        const detail = value?.detail
        if (detail && typeof detail === 'object' && ['read_timeout', 'read_busy', 'read_cancelled'].includes(detail.code)) {
          if (detail.code === 'read_cancelled') throw new DOMException('读取已取消', 'AbortError')
          if (detail.code === 'read_busy' && canRetry && !busyRetried && attempt < 2) {
            busyRetried = true
            const header = response.headers.get('Retry-After')
            const delay = Math.min(3000, (header?.trim() ? retryDelay(header, 0) : 250) + Math.floor(Math.random() * 201))
            await waitForRetry(delay, signal)
            continue
          }
          throw new ApiReadError(typeof detail.message === 'string' && detail.message ? detail.message : httpError(response.status), detail.code)
        }
      }
      if (canRetry && attempt < 2 && [502, 503, 504].includes(response.status)) {
        const delay = retryDelay(response.headers.get('Retry-After'), attempt)
        try { if (response.body) await untilAborted(response.body.cancel(), signal) } catch { /* Failed body cleanup must not replace the HTTP failure. */ }
        signal.throwIfAborted()
        await waitForRetry(delay, signal)
        continue
      }
      if (!parsed) {
        try {
          value = await untilAborted(response.json(), signal)
        } catch (error) {
          signal.throwIfAborted()
          if (response.ok && error instanceof TypeError) throw error
          throw new Error(response.ok ? '服务返回的数据无法读取，请重试' : httpError(response.status))
        }
      }
      signal.throwIfAborted()
      if (!response.ok || value?.ok === false) {
        if (value?.detail?.code === 'cursor_expired') throw new ApiReadError(value.detail.message || '图库已变化，请重新定位后继续', 'cursor_expired')
        const message = typeof value?.detail === 'string' ? value.detail : value?.detail?.message || value?.error
        throw new Error(typeof message === 'string' && message ? message : httpError(response.status))
      }
      return value as T
    }
  } catch (error) {
    if (options.signal?.aborted) throw options.signal.reason
    if (signal.aborted) throw new DOMException(canRetry ? '读取超时，请稍后重试' : '请求超时，操作结果尚未确认，请查询状态后再试', 'TimeoutError')
    if (error instanceof TypeError) throw new TypeError(canRetry ? '无法连接本地服务，请检查程序是否仍在运行' : '无法连接本地服务，操作结果尚未确认，请查询状态后再试')
    throw error
  }
}
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason) }
    signal.addEventListener('abort', abort, {once: true})
    promise.then(value => { signal.removeEventListener('abort', abort); resolve(value) }, error => {
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
export function prepareImage(src: string | null, signal: AbortSignal): Promise<void> {
  if (!src) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const image = new Image()
    let settled = false
    const cleanup = () => { clearTimeout(timer); image.onload = null; image.onerror = null; signal.removeEventListener('abort', abort) }
    const finish = (failure?: Error) => {
      if (settled) return
      settled = true; cleanup()
      if (failure) { image.removeAttribute('src'); reject(failure) }
      else resolve()
    }
    const abort = () => finish(new DOMException('Aborted', 'AbortError'))
    const timer = setTimeout(() => finish(new DOMException('目标图片等待超时，当前画面已保留，请重试', 'TimeoutError')), 10000)
    image.onload = async () => {
      try { await image.decode() } catch { /* onload already confirms a renderable image */ }
      if (signal.aborted) { abort(); return }
      finish()
    }
    image.onerror = () => finish(new Error('目标图片未能载入，当前画面已保留，请重试'))
    signal.addEventListener('abort', abort, {once: true})
    if (signal.aborted) abort()
    else image.src = src
  })
}
