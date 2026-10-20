import { reactive } from 'vue'
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
const pollFlights = new Map<number, AbortController>()
const inFlight = reactive(new Set<number>())
const writing = reactive(new Set<number>())
const lastRefresh = new Map<number, number>()
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
export function bookmarkBusy(id: number) { return writing.has(id) || states.get(id)?.status === 'pending' }
export function bookmarkWriting(id: number) {
  const state = states.get(id)
  return writing.has(id) || state?.status === 'pending' && (state.kind === 'write' || typeof state.desired === 'boolean')
}
export function bookmarkAcknowledging(id: number) { return feedback.active(id) }
function trim() {
  if (states.size <= 600) return
  for (const [id, state] of states) {
    if (states.size <= 600) break
    if (state.status !== 'pending' && !inFlight.has(id) && !writing.has(id) && !feedback.active(id)) { states.delete(id); lastRead.delete(id); lastRefresh.delete(id) }
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
  // A transport failure has no accepted operation ID. An older background GET
  // cannot prove that this write was never accepted; only a new readback or
  // the original operation's terminal result can clear the uncertainty.
  if (previous?.local_unknown && !previous.operation_id && accountEpoch === previous.account_epoch &&
      version <= previous.version) return previous
  const normalized: BookmarkState = {
    work_id: id, operation_id: typeof state.operation_id === 'string' ? state.operation_id : null,
    status: state.status || 'idle', desired: typeof state.desired === 'boolean' ? state.desired : null,
    value, confirmed_at: state.confirmed_at || null, error: state.error || null,
    version, cached: !!state.cached, verified: state.verified === true,
    fact_account_id: typeof state.fact_account_id === 'string' ? state.fact_account_id : null,
    current_account_id: currentAccountId, account_epoch: accountEpoch,
    kind: state.kind === 'write' || state.kind === 'refresh' ? state.kind : null,
    local_unknown: false,
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
  pollFlights.get(id)?.abort()
  pollFlights.delete(id)
}
function poll(id: number, operationId: string, attempt = 0) {
  if (polling.has(id) || pollFlights.has(id) || document.hidden) return
  const timer = window.setTimeout(async () => {
    polling.delete(id)
    const controller = new AbortController()
    pollFlights.set(id, controller)
    let retry = false
    try {
      const response = await api<unknown>(`/api/bookmark-operations/${encodeURIComponent(operationId)}`, {signal: controller.signal})
      const current = states.get(id)
      if (controller.signal.aborted || current?.operation_id !== operationId) return
      const state = ingestBookmark(id, response, false)
      retry = state?.status === 'pending'
    } catch (failure) {
      if (controller.signal.aborted) return
      const current = states.get(id)
      if (current?.operation_id === operationId && current.status === 'pending') {
        if (attempt >= 8) markUnknown(id, '暂时无法读取收藏结果，点击可重新核对；不会重复写入')
        else states.set(id, {...current, error: failure instanceof Error ? `${failure.message}；稍后继续核对` : '收藏状态暂时无法读取，稍后继续核对'})
      }
      retry = attempt < 8 && states.get(id)?.operation_id === operationId
    } finally {
      if (pollFlights.get(id) === controller) pollFlights.delete(id)
      if (retry && !controller.signal.aborted) poll(id, operationId, attempt + 1)
    }
  }, Math.min(9000, 900 + attempt * 700))
  polling.set(id, timer)
}
function markUnknown(id: number, error: string, missingReceipt = false) {
  const current = states.get(id)
  if (!current) return
  const state: BookmarkState = {...current, status: 'unconfirmed', local_unknown: true,
    cached: current.value !== null, error, ...(missingReceipt ? {operation_id: null} : {})}
  states.set(id, state)
  feedback.observe(id, state)
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
  if (state?.verified && state.value === true && state.fact_account_id === state.current_account_id &&
      state.status !== 'unconfirmed' && !state.local_unknown) {
    const token = feedback.start(id, state)
    if (token !== undefined) feedback.receipt(id, token, state.status === 'pending' ? undefined : state)
    return state
  }
  if (bookmarkWriting(id) || feedback.active(id)) return state
  return toggleBookmark(id, true)
}
export async function toggleBookmark(id: number, explicitDesired?: boolean) {
  const state = states.get(id)
  if (bookmarkWriting(id) || feedback.active(id)) return state
  if (!state?.current_account_id) throw new Error('请先连接 Pixiv，再核对当前账号的收藏状态')
  const desired = explicitDesired ?? nextBookmarkIntent(state)
  const identity = requestId()
  const feedbackToken = feedback.start(id, state)!
  const accountEpoch = state.account_epoch
  const account = state.current_account_id
  const recovering = state.local_unknown || state.status === 'unconfirmed'
  writing.add(id)
  try {
    // The target, account and request ID are captured before awaiting anything.
    // Background reads cannot change this intent or masquerade as its receipt.
    const response = recovering ? await write<unknown>(`/api/works/${id}/bookmark/refresh`, {}) :
      await write<unknown>(`/api/works/${id}/bookmark`, {
        desired, request_id: identity, expected_account_id: account,
      })
    const result = ingestBookmark(id, response)
    feedback.receipt(id, feedbackToken, result)
    if (result?.status === 'pending' && result.operation_id) poll(id, result.operation_id)
    return result
  } catch (failure) {
    const current = states.get(id)
    if (current?.account_epoch === accountEpoch && current.current_account_id === account) {
      markUnknown(id, '收藏结果尚未确认，点击可重新核对；不会重复写入', true)
      feedback.receipt(id, feedbackToken, undefined)
      // Error reporting must not wait for a second network request.
      void readBookmark(id, true).catch(() => undefined)
    }
    throw failure
  } finally { writing.delete(id) }
}
export function resumeBookmarkPolling() {
  for (const [id, state] of states) if ((state.status === 'pending' || state.local_unknown) && state.operation_id) poll(id, state.operation_id)
}
