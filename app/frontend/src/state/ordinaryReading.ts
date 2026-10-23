import { SharedReads } from '../interaction.ts'
import type { WorkDetail } from '../types.ts'

export interface OrdinaryNeighbor {work_id: number; revision_id: string | null; page_count: number; cursor: string}
export interface CursorNeighbors {anchor_cursor: string; generation: number; continuation_rebased?: boolean; previous: OrdinaryNeighbor | null; next: OrdinaryNeighbor | null}
export interface OrdinaryTarget {id: number; cursor?: string; revision_id?: string | null}
export interface OrdinarySource {cursor: string; direction: 'previous' | 'next'}
export const ORDINARY_OPENING_PAGES = 4
interface Readers {
  neighbors: (path: string, signal: AbortSignal, readScope: string) => Promise<CursorNeighbors>
  opening: (id: number, revision: string | null | undefined, signal: AbortSignal, readScope: string) => Promise<WorkDetail>
}

/** Only initial display may consume prepared metadata; retries and repairs read live state. */
export function ordinaryOpeningWindow(work: WorkDetail) {
  let initial = true
  return (offset: number, limit: number, signal: AbortSignal): WorkDetail | null => {
    signal.throwIfAborted()
    const reusable = initial && offset === 0 && work.pages.length > 0
    initial = false
    return reusable ? {...work, pages: work.pages.slice(0, Math.min(limit, ORDINARY_OPENING_PAGES))} : null
  }
}

/** Exactly the same request identity for an adjacent prefetch and its navigation. */
export function ordinaryNeighborPath(query: string, target: OrdinaryTarget, source?: OrdinarySource): string {
  const params = new URLSearchParams(query)
  if (target.cursor) params.set('cursor', target.cursor)
  if (source) { params.set('from_cursor', source.cursor); params.set('from_direction', source.direction) }
  return `/api/works/${target.id}/cursor-neighbors?${params}`
}

/** A small ordinary-reader window; catalogue/scope changes, not elapsed reading time, expire it. */
export class OrdinaryReadingCache {
  private neighbors = new Map<string, CursorNeighbors>()
  private openings = new Map<string, WorkDetail>()
  private reads = new SharedReads()
  private epoch = 0
  private readonly readers: Readers

  constructor(readers: Readers) { this.readers = readers }

  clear() { this.epoch++; this.neighbors.clear(); this.openings.clear() }

  readNeighbors(query: string, target: OrdinaryTarget, generation: number | undefined, signal: AbortSignal, source?: OrdinarySource) {
    const path = ordinaryNeighborPath(query, target, source)
    const key = JSON.stringify([path, target.revision_id ?? null, generation ?? null])
    return this.read(this.neighbors, `neighbors:${key}`, key, signal,
      (owned, scope) => this.readers.neighbors(path, owned, scope))
  }

  readOpening(query: string, id: number, revision: string | null | undefined, signal: AbortSignal) {
    const key = JSON.stringify([query, id, revision ?? null])
    return this.read(this.openings, `opening:${key}`, key, signal, (owned, scope) => this.readers.opening(id, revision, owned, scope))
  }

  private read<T>(cache: Map<string, T>, flight: string, key: string, signal: AbortSignal,
    start: (signal: AbortSignal, scope: string) => Promise<T>): Promise<T> {
    if (signal.aborted) return Promise.reject(signal.reason)
    const ready = cache.get(key)
    if (ready) { cache.delete(key); cache.set(key, ready); return Promise.resolve(ready) }
    const epoch = this.epoch, scope = `ordinary:${epoch}:${flight}`
    return this.reads.read(scope, async owned => {
      const value = await start(owned, scope)
      owned.throwIfAborted()
      if (epoch === this.epoch) {
        cache.set(key, value)
        while (cache.size > 3) cache.delete(cache.keys().next().value!)
      }
      return value
    }, signal)
  }
}
