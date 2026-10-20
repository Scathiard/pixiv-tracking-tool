import type {Anchor, PageData} from '../types.ts'

export const STREAM_PAGE_LIMIT = 120
export const STREAM_PULL_DISTANCE = 64
export const STREAM_WHEEL_QUIET = 220

/** Reaching an edge during a swipe never arms that same swipe to change works. */
export function streamTouchDirection(delta: number, startedAtTop: boolean, startedAtBottom: boolean): number {
  if (Math.abs(delta) < STREAM_PULL_DISTANCE) return 0
  const direction = delta > 0 ? -1 : 1
  return (direction < 0 ? startedAtTop : startedAtBottom) ? direction : 0
}

export function sameStreamWork(page: PageData, target: PageData): boolean {
  return page.work_id === target.work_id && page.revision_id === target.revision_id
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

/** This visit's recent positions. It never stores media or persists across visits. */
export class StreamPositions {
  private anchors = new Map<string, Anchor>()
  private key(page: PageData) { return `${page.work_id}:${page.revision_id ?? ''}` }
  remember(page: PageData, anchor: Anchor) {
    const key = this.key(page)
    this.anchors.delete(key); this.anchors.set(key, {...anchor})
    while (this.anchors.size > 80) this.anchors.delete(this.anchors.keys().next().value!)
  }
  read(page: PageData): Anchor | null {
    const anchor = this.anchors.get(this.key(page))
    // Cursor identity windows may rebase ordinals while retaining work identity.
    return anchor ? {...anchor, index: page.index - page.page + anchor.page, work_index: page.work_index} : null
  }
  clear() { this.anchors.clear() }
}

/** A wheel burst can navigate only if its very first event began at that edge. */
export class StreamWheel {
  private last = -Infinity
  private direction = 0
  private distance = 0
  private eligible = false
  private consumed = false
  feed(delta: number, time: number, edge: (direction: number) => boolean): number {
    const direction = Math.sign(delta)
    if (!direction) return 0
    if (time - this.last > STREAM_WHEEL_QUIET) {
      this.direction = direction; this.distance = 0; this.consumed = false
      this.eligible = edge(direction)
    }
    this.last = time
    if (direction !== this.direction || !edge(direction)) this.eligible = false
    if (!this.eligible || this.consumed) return 0
    this.distance += Math.abs(delta)
    if (this.distance < STREAM_PULL_DISTANCE) return 0
    this.consumed = true
    return direction
  }
  block(time: number) { this.last = time; this.eligible = false; this.consumed = true }
  reset() { this.last = -Infinity; this.eligible = false; this.consumed = false; this.distance = 0 }
}
