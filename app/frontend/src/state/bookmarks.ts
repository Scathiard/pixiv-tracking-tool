import { reactive } from 'vue'
import { api, write } from '../api'
import type { BookmarkState } from '../types'
import { acceptsBookmarkUpdate } from './bookmarkRules'
import { requestId } from './requestId'

const states = reactive(new Map<number, BookmarkState>())
const lastRead = new Map<number, number>()
const polling = new Map<number, number>()
const inFlight = reactive(new Set<number>())
const lastRefresh = new Map<number, number>()
let lastRemoteRefresh = 0, refreshTimer = 0, queuedRefresh = 0
type Incoming = Partial<BookmarkState> & {work_id?: number}

function isState(value: unknown): value is Incoming {
  return !!value && typeof value === 'object' && 'status' in value && 'version' in value
}
function unwrap(value: unknown): Incoming | null {
  if (isState(value)) return value
  if (value && typeof value === 'object') {
    const object = value as Record<string, unknown>
    if (isState(object.bookmark)) return object.bookmark
    if (isState(object.state)) return object.state
  }
  return null
}
export function bookmarkState(id: number) { return states.get(id) }
export function bookmarkBusy(id: number) { return inFlight.has(id) || states.get(id)?.status === 'pending' }
function trim() {
  if (states.size <= 600) return
  for (const [id, state] of states) {
    if (states.size <= 600) break
    if (state.status !== 'pending' && !inFlight.has(id)) { states.delete(id); lastRead.delete(id); lastRefresh.delete(id) }
  }
}
export function ingestBookmark(id: number, incoming: unknown) {
  const state = unwrap(incoming)
  if (!state) return states.get(id)
  const version = Number(state.version)
  if (!Number.isSafeInteger(version) || version < 0) return states.get(id)
  const previous = states.get(id)
  if (!acceptsBookmarkUpdate(previous, {version, status: state.status || 'idle', operation_id: state.operation_id})) return previous
  const value = typeof state.value === 'boolean' ? state.value : null
  const normalized: BookmarkState = {
    work_id: id, operation_id: typeof state.operation_id === 'string' ? state.operation_id : null,
    status: state.status || 'idle', desired: typeof state.desired === 'boolean' ? state.desired : null,
    value, confirmed_at: state.confirmed_at || null, error: state.error || null,
    version, cached: !!state.cached,
  }
  states.delete(id); states.set(id, normalized); trim()
  if (normalized.status === 'pending' && normalized.operation_id) poll(id, normalized.operation_id)
  else stopPoll(id)
  return normalized
}
function stopPoll(id: number) {
  const timer = polling.get(id)
  if (timer !== undefined) clearTimeout(timer)
  polling.delete(id)
}
function poll(id: number, operationId: string, attempt = 0) {
  if (polling.has(id) || document.hidden) return
  const timer = window.setTimeout(async () => {
    polling.delete(id)
    try {
      const response = await api<unknown>(`/api/bookmark-operations/${encodeURIComponent(operationId)}`)
      const current = states.get(id)
      if (current?.operation_id !== operationId) return
      const state = ingestBookmark(id, response)
      if (state?.status === 'pending') poll(id, operationId, attempt + 1)
    } catch {
      const current = states.get(id)
      if (current?.operation_id === operationId && current.status === 'pending') states.set(id, {...current, error: '暂时无法联系 Pixiv，稍后继续核对'})
      if (attempt < 8 && states.get(id)?.operation_id === operationId) poll(id, operationId, attempt + 1)
    }
  }, Math.min(9000, 900 + attempt * 700))
  polling.set(id, timer)
}
export async function readBookmark(id: number, force = false) {
  if (inFlight.has(id) || (!force && Date.now() - (lastRead.get(id) || 0) < 45000)) return states.get(id)
  lastRead.set(id, Date.now())
  inFlight.add(id)
  try { return ingestBookmark(id, await api<unknown>(`/api/works/${id}/bookmark`)) }
  finally { inFlight.delete(id) }
}
export async function syncBookmark(id: number) {
  if (inFlight.has(id)) throw new Error('正在核对收藏状态，请稍后再试')
  lastRead.set(id, Date.now())
  inFlight.add(id)
  try { return ingestBookmark(id, await write<unknown>(`/api/works/${id}/bookmark/refresh`, {})) }
  finally { inFlight.delete(id) }
}
export function refreshBookmark(id: number) {
  if (Date.now() - (lastRefresh.get(id) || 0) < 45000) return
  queuedRefresh = id
  clearTimeout(refreshTimer)
  const delay = Math.max(0, 2500 - (Date.now() - lastRemoteRefresh))
  refreshTimer = window.setTimeout(() => {
    const current = queuedRefresh
    queuedRefresh = 0
    lastRemoteRefresh = Date.now(); lastRefresh.set(current, lastRemoteRefresh)
    void syncBookmark(current).catch(() => undefined)
  }, delay)
}
export async function toggleBookmark(id: number) {
  const state = states.get(id)
  if (inFlight.has(id) || state?.status === 'pending') throw new Error('正在核对收藏状态，请稍后再试')
  inFlight.add(id)
  try {
    const result = ingestBookmark(id, await write<unknown>(`/api/works/${id}/bookmark`, {
      desired: typeof state?.value === 'boolean' ? !state.value : true, request_id: requestId(),
    }))
    if (result?.status === 'pending' && result.operation_id) poll(id, result.operation_id)
    return result
  } catch (failure) {
    inFlight.delete(id)
    await readBookmark(id, true).catch(() => undefined)
    throw failure
  } finally { inFlight.delete(id) }
}
export function resumeBookmarkPolling() {
  for (const [id, state] of states) if (state.status === 'pending' && state.operation_id) poll(id, state.operation_id)
}
