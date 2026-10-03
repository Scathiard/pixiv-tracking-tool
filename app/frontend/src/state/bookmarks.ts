import { reactive, watch } from 'vue'
import { api, write } from '../api'
import type { BookmarkState } from '../types'
import { acceptsBookmarkUpdate, nextBookmarkIntent } from './bookmarkRules'
import { requestId } from './requestId'
import { createBookmarkFeedback, type BookmarkFeedback } from './bookmarkFeedback'

const states = reactive(new Map<number, BookmarkState>())
const feedback = createBookmarkFeedback({now: () => performance.now(),
  setTimer: (callback, delay) => window.setTimeout(callback, delay),
  clearTimer: timer => window.clearTimeout(timer)}, reactive(new Map<number, BookmarkFeedback>()))
const lastRead = new Map<number, number>()
const polling = new Map<number, number>()
const inFlight = reactive(new Set<number>())
const lastRefresh = new Map<number, number>()
const queuedAdds = new Map<number, Promise<BookmarkState | undefined>>()
let lastRemoteRefresh = 0, refreshTimer = 0, queuedRefresh = 0
let latestAccountEpoch = 0
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
export function bookmarkAcknowledging(id: number) { return feedback.active(id) }
function trim() {
  if (states.size <= 600) return
  for (const [id, state] of states) {
    if (states.size <= 600) break
    if (state.status !== 'pending' && !inFlight.has(id) && !feedback.active(id)) { states.delete(id); lastRead.delete(id); lastRefresh.delete(id) }
  }
}
export function ingestBookmark(id: number, incoming: unknown, schedulePolling = true) {
  const state = unwrap(incoming)
  if (!state) return states.get(id)
  const version = Number(state.version)
  if (!Number.isSafeInteger(version) || version < 0) return states.get(id)
  const accountEpoch = Number(state.account_epoch ?? 0)
  if (!Number.isSafeInteger(accountEpoch) || accountEpoch < 0) return states.get(id)
  const currentAccountId = typeof state.current_account_id === 'string' ? state.current_account_id : null
  if (accountEpoch < latestAccountEpoch) return states.get(id)
  if (accountEpoch > latestAccountEpoch) {
    latestAccountEpoch = accountEpoch
    feedback.clear()
    lastRead.clear(); lastRefresh.clear()
    clearTimeout(refreshTimer); queuedRefresh = 0; lastRemoteRefresh = 0
    for (const [workId, previousState] of states) {
      stopPoll(workId)
      if (workId === id) continue
      states.set(workId, {...previousState, current_account_id: currentAccountId,
        account_epoch: accountEpoch, cached: true, operation_id: null, desired: null,
        status: previousState.verified ? 'confirmed' : 'idle'})
    }
  }
  const previous = states.get(id)
  if (!acceptsBookmarkUpdate(previous, {version, status: state.status || 'idle', operation_id: state.operation_id,
    account_epoch: accountEpoch, current_account_id: currentAccountId})) return previous
  const value = typeof state.value === 'boolean' ? state.value : null
  const normalized: BookmarkState = {
    work_id: id, operation_id: typeof state.operation_id === 'string' ? state.operation_id : null,
    status: state.status || 'idle', desired: typeof state.desired === 'boolean' ? state.desired : null,
    value, confirmed_at: state.confirmed_at || null, error: state.error || null,
    version, cached: !!state.cached, verified: state.verified === true,
    fact_account_id: typeof state.fact_account_id === 'string' ? state.fact_account_id : null,
    current_account_id: currentAccountId, account_epoch: accountEpoch,
  }
  states.delete(id); states.set(id, normalized); trim()
  feedback.observe(id, normalized)
  if (normalized.status === 'pending' && normalized.operation_id) {
    if (schedulePolling) poll(id, normalized.operation_id)
  }
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
      const state = ingestBookmark(id, response, false)
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
export async function syncBookmark(id: number, automatic = false) {
  if (inFlight.has(id)) throw new Error('正在核对收藏状态，请稍后再试')
  lastRead.set(id, Date.now())
  inFlight.add(id)
  try { return ingestBookmark(id, await write<unknown>(`/api/works/${id}/bookmark/refresh${automatic ? '?automatic=1' : ''}`, {})) }
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
    void syncBookmark(current, true).catch(() => undefined)
  }, delay)
}
export async function addBookmark(id: number) {
  const state = states.get(id)
  if (state?.verified && state.value === true && state.fact_account_id === state.current_account_id) return state
  if (queuedAdds.has(id)) return queuedAdds.get(id)
  if (state?.desired === true && state.status === 'pending' || feedback.active(id)) return state
  const account = state?.current_account_id
  // A background read must not swallow a deliberate double tap. Wait for that
  // read once; repeated taps share the same intent and never replay a write.
  const pending = (async () => {
    if (bookmarkBusy(id)) await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => { stop(); reject(new Error('收藏核对仍未完成，请稍后重试')) }, 30000)
      const stop = watch(() => bookmarkBusy(id), busy => {
        if (!busy) { clearTimeout(timer); stop(); resolve() }
      }, {flush: 'sync'})
    })
    const current = states.get(id)
    if (current?.current_account_id !== account) throw new Error('登录账号已变化，请重新确认收藏')
    if (current?.verified && current.value === true && current.fact_account_id === account) return current
    return toggleBookmark(id, true)
  })()
  queuedAdds.set(id, pending)
  try { return await pending } finally { if (queuedAdds.get(id) === pending) queuedAdds.delete(id) }
}
export async function toggleBookmark(id: number, explicitDesired?: boolean) {
  const state = states.get(id)
  if (inFlight.has(id) || state?.status === 'pending' || feedback.active(id)) throw new Error('正在核对收藏状态，请稍后再试')
  if (!state?.current_account_id) throw new Error('请先连接 Pixiv，再核对当前账号的收藏状态')
  const desired = explicitDesired ?? nextBookmarkIntent(state)
  const feedbackToken = feedback.start(id, state)!
  inFlight.add(id)
  try {
    const result = ingestBookmark(id, await write<unknown>(`/api/works/${id}/bookmark`, {
      desired, request_id: requestId(), expected_account_id: state.current_account_id,
    }))
    feedback.receipt(id, feedbackToken, result)
    if (result?.status === 'pending' && result.operation_id) poll(id, result.operation_id)
    return result
  } catch (failure) {
    inFlight.delete(id)
    const recovered = await readBookmark(id, true).catch(() => undefined)
    feedback.receipt(id, feedbackToken, recovered)
    throw failure
  } finally { inFlight.delete(id) }
}
export function resumeBookmarkPolling() {
  for (const [id, state] of states) if (state.status === 'pending' && state.operation_id) poll(id, state.operation_id)
}
