import type {PageBatch, PageData, PageLoader} from '../types.ts'

export const STREAM_PAGE_LIMIT = 120
export const STREAM_PULL_DISTANCE = 120
export const STREAM_WHEEL_DISTANCE = 64
export const STREAM_WHEEL_QUIET = 220

export function sameStreamWork(page: PageData, target: PageData): boolean {
  return page.work_id === target.work_id && page.revision_id === target.revision_id && (page.round || 0) === (target.round || 0)
}

/** Keep only one revision, bounded metadata, and original global page ordinals. */
export function streamPageWindow(items: readonly PageData[], target: PageData, direction = 0): PageData[] {
  const unique = new Map<number, PageData>()
  for (const page of items) if (sameStreamWork(page, target)) unique.set(page.index, page)
  const sorted = [...unique.values()].sort((left, right) => left.index - right.index)
  if (direction > 0) return sorted.slice(-STREAM_PAGE_LIMIT)
  if (direction < 0) return sorted.slice(0, STREAM_PAGE_LIMIT)
  const selected = Math.max(0, sorted.findIndex(page => page.index === target.index))
  const start = Math.max(0, Math.min(selected - 32, sorted.length - STREAM_PAGE_LIMIT))
  return sorted.slice(start, start + STREAM_PAGE_LIMIT)
}

export interface StreamInitialOpening {sessionKey: string; batch: PageBatch}
/** Each panel keeps its own coordinate source through cursor rebases and reverse navigation. */
export interface StreamCandidate {
  batch: PageBatch; target: PageData; loader: PageLoader; prepareNeighbor: StreamNeighborLoader
  commit: () => void; tail?: boolean
}
export type StreamNeighborLoader = (page: PageData, direction: number, signal: AbortSignal) => Promise<StreamCandidate | null>
export function streamIdentity(page: PageData): string {
  return [page.round || 0, page.work_id, page.revision_id ?? ''].join(':')
}
/** Touch follows the finger across a full viewport; only overtravel is resisted. */
export function streamDragOffset(delta: number, height: number): number {
  const distance = Math.abs(delta), extent = Math.max(1, height)
  return Math.sign(delta) * Math.min(extent + Math.min(64, (distance - extent) * .2), distance)
}
export function streamCanCommit(distance: number, ready: boolean, cancelled = false, threshold = STREAM_PULL_DISTANCE): boolean {
  return ready && !cancelled && Math.abs(distance) >= threshold
}
/** A wheel burst retains the edge qualification of its first event, including inertia. */
export class StreamWheelDrag {
  private last = -Infinity
  private eligible = false
  private direction = 0
  private distance = 0
  feed(delta: number, time: number, edge: (direction: number) => boolean): number {
    const direction = Math.sign(delta)
    if (!direction) return 0
    if (time - this.last > STREAM_WHEEL_QUIET) {
      this.eligible = edge(direction); this.direction = direction; this.distance = 0
    }
    this.last = time
    if (direction !== this.direction || !edge(direction)) this.eligible = false
    if (!this.eligible) return 0
    this.distance -= delta
    return this.distance
  }
  block(time: number) { this.last = time; this.eligible = false; this.distance = 0 }
}
