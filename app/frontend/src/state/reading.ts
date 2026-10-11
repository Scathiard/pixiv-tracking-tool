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

/** A rebased identity window has no negative ordinal for its predecessor. */
export function needsIdentityPrevious(page: PageData): boolean {
  return page.previous_work_index === 0 && page.index - page.page === 0
}
