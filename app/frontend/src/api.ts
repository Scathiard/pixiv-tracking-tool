export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const signal = options.signal
    ? AbortSignal.any([options.signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000)
  const canRetry = (options.method || 'GET').toUpperCase() === 'GET'
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted()
    let response: Response
    try {
      response = await fetch(path, {...options, signal})
    } catch (error) {
      signal.throwIfAborted()
      if (canRetry && attempt < 2 && error instanceof TypeError) {
        await waitForRetry(attempt === 0 ? 350 : 1000, signal)
        continue
      }
      throw error
    }
    signal.throwIfAborted()
    if (canRetry && attempt < 2 && [502, 503, 504].includes(response.status)) {
      const delay = retryDelay(response.headers.get('Retry-After'), attempt)
      try { if (response.body) await response.body.cancel() } catch { /* Failed body cleanup must not replace the HTTP failure. */ }
      signal.throwIfAborted()
      await waitForRetry(delay, signal)
      continue
    }
    let value
    try {
      value = await response.json()
    } catch {
      signal.throwIfAborted()
      throw new Error(response.ok ? '服务返回的数据无法读取，请重试' : httpError(response.status))
    }
    signal.throwIfAborted()
    if (!response.ok || value?.ok === false) {
      const message = typeof value?.detail === 'string' ? value.detail : value?.error
      throw new Error(typeof message === 'string' && message ? message : httpError(response.status))
    }
    return value as T
  }
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
    const cleanup = () => { image.onload = null; image.onerror = null; signal.removeEventListener('abort', abort) }
    const abort = () => { cleanup(); image.removeAttribute('src'); reject(new DOMException('Aborted', 'AbortError')) }
    image.onload = async () => {
      try { await image.decode() } catch { /* onload already confirms a renderable image */ }
      if (signal.aborted) { abort(); return }
      cleanup(); resolve()
    }
    image.onerror = () => { cleanup(); reject(new Error('目标图片未能载入，当前画面已保留')) }
    signal.addEventListener('abort', abort, {once: true})
    if (signal.aborted) abort()
    else image.src = src
  })
}
