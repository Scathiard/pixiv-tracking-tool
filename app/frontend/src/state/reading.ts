import { api, url } from '../api.ts'
import type { Anchor, PageBatch, PageData } from '../types.ts'
export type ReadingOrder = 'sequential' | 'random'
export interface ReadingVisit {session_id: string; total: number; page_total: number}
export interface ReadingEntry {work_id: number; revision_id: string | null; work_index: number; page_count: number; total: number}
export function startVisit(query: string, signal: AbortSignal) {
  return api<ReadingVisit>(url('/api/reading-sessions', query), {method: 'POST', signal})
}
export function releaseVisit(visit: ReadingVisit | null) {
  if (visit) void api(`/api/reading-sessions/${visit.session_id}`, {method: 'DELETE'}).catch(() => undefined)
}
export function visitEntry(visit: ReadingVisit, index: number, signal: AbortSignal) {
  return api<ReadingEntry>(url(`/api/reading-sessions/${visit.session_id}/entry`, '', {index}), {signal})
}
export function locateVisit(visit: ReadingVisit, workId: number, order: ReadingOrder, round: number, signal: AbortSignal) {
  return api<Anchor>(url(`/api/reading-sessions/${visit.session_id}/locate`, '', {work_id: workId, mode: order, round}), {signal})
}
export function visitPages(visit: ReadingVisit, order: ReadingOrder, offset: number, limit: number, signal: AbortSignal) {
  return api<PageBatch>(url(`/api/reading-sessions/${visit.session_id}/feed`, '', {mode: order, offset, limit}), {signal})
}

interface RandomPreparationIO {
  start: typeof startVisit
  release: typeof releaseVisit
}
interface RandomSlot {
  query: string; controller: AbortController; promise: Promise<ReadingVisit>
  ready: ReadingVisit | null; foreground: number
}

/** Scope-owned list preparation. Per-work locate cancellation never cancels the shared list. */
export class RandomReadingPreparation {
  private slot: RandomSlot | null = null
  private readonly io: RandomPreparationIO
  constructor(io: RandomPreparationIO = {start: startVisit, release: releaseVisit}) { this.io = io }
  warm(query: string) { return this.get(query).promise }
  peek(query: string) { return this.slot?.query === query ? this.slot.ready : null }
  async take(query: string, signal: AbortSignal): Promise<ReadingVisit> {
    signal.throwIfAborted()
    const slot = this.get(query)
    slot.foreground++
    try {
      const visit = await waitForPreparation(slot.promise, signal)
      signal.throwIfAborted()
      if (this.slot !== slot) throw new DOMException('阅读范围已切换', 'AbortError')
      // The caller now owns release, including when it later switches back to sequential.
      this.slot = null
      return visit
    } finally {
      slot.foreground--
    }
  }
  pause() { /* Only clear() at scope/exit owns cancellation of shared preparation. */ }
  clear() {
    const slot = this.slot; this.slot = null
    if (slot) { slot.controller.abort(); if (slot.ready) this.io.release(slot.ready) }
  }
  private get(query: string): RandomSlot {
    if (this.slot?.query === query) return this.slot
    this.clear()
    const controller = new AbortController()
    const slot: RandomSlot = {query, controller, ready: null, foreground: 0, promise: null!}
    this.slot = slot
    slot.promise = (async () => {
      let visit: ReadingVisit | null = null
      try {
        visit = await this.io.start(query, controller.signal)
        controller.signal.throwIfAborted()
        if (this.slot !== slot) throw new DOMException('阅读范围已切换', 'AbortError')
        slot.ready = visit
        return visit
      } catch (failure) {
        if (visit) this.io.release(visit)
        if (this.slot === slot) this.slot = null
        throw failure
      }
    })()
    return slot
  }
}

function waitForPreparation<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason) }
    signal.addEventListener('abort', abort, {once: true})
    promise.then(value => { signal.removeEventListener('abort', abort); resolve(value) }, error => {
      signal.removeEventListener('abort', abort); reject(error)
    })
    if (signal.aborted) abort()
  })
}

/** Existing routes return 410 text; 404 also covers an older service's expired token. */
export function expiredReadingVisit(failure: unknown): boolean {
  return failure instanceof Error && (/^阅读范围已过期/.test(failure.message) || /[（(](404|410)[）)]/.test(failure.message))
}

/** A rebased identity window has no negative ordinal for its predecessor. */
export function needsIdentityPrevious(page: PageData): boolean {
  return page.previous_work_index === 0 && page.index - page.page === 0
}
