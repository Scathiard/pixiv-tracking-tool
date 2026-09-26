export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {...options, signal: options.signal
    ? AbortSignal.any([options.signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000)})
  const value = await response.json()
  if (!response.ok || value.ok === false) {
    const message = typeof value.detail === 'string' ? value.detail : value.error
    throw new Error(message || `操作未完成（${response.status}），请重试`)
  }
  return value as T
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
