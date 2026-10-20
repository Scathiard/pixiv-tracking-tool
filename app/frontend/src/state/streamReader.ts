import type {Anchor, PageBatch, PageData} from '../types.ts'
import type {CursorPageBatch} from './cursorReading.ts'

export const STREAM_PAGE_LIMIT = 120
export const STREAM_PULL_DISTANCE = 120
export const STREAM_WHEEL_DISTANCE = 64
export const STREAM_NEARBY_WORKS = 5
export const STREAM_OPENING_PAGES = 4
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

/** Short opening windows only; session ownership is held by the reader instance. */
export class StreamOpenings {
  private works = new Map<number, PageBatch>()
  remember(batch: PageBatch, target: PageData) {
    const items = batch.items.filter(page => sameStreamWork(page, target) &&
      page.index - page.page === target.index - target.page).sort((a, b) => a.index - b.index)
    const selected = items.findIndex(page => page.index === target.index)
    if (selected < 0) return
    const start = Math.max(0, Math.min(selected - 1, items.length - STREAM_OPENING_PAGES))
    const key = target.index - target.page
    this.works.delete(key)
    this.works.set(key, {...batch, items: items.slice(start, start + STREAM_OPENING_PAGES).map(page => ({...page}))})
    while (this.works.size > STREAM_NEARBY_WORKS) this.works.delete(this.works.keys().next().value!)
  }
  read(index: number, catalogueRevision: number | undefined): PageBatch | null {
    for (const batch of this.works.values()) {
      if (batch.catalogue_revision !== catalogueRevision || !batch.items.some(page => page.index === index)) continue
      // Repair must re-read the server; never turn a failed/missing page into a permanent cache hit.
      if (!batch.items.every(page => page.src && page.state === 'verified' &&
        !['queued', 'waiting'].includes(page.recovery?.status || ''))) return null
      return {...batch, items: batch.items.map(page => ({...page}))}
    }
    return null
  }
  get size() { return this.works.size }
  get retainedPages() { return [...this.works.values()].reduce((total, batch) => total + batch.items.length, 0) }
  clear() { this.works.clear() }
}

export interface StreamInitialOpening {sessionKey: string; batch: PageBatch}
export interface StreamPreviousOwner {
  sessionKey: string; scope: string; workId: number; revisionId: string | null | undefined
  cursor: string | undefined; generation: number | undefined
}
function previousOwnerKey(owner: StreamPreviousOwner) {
  return JSON.stringify([owner.sessionKey, owner.scope, owner.workId, owner.revisionId ?? null, owner.cursor, owner.generation])
}

/** A rebased predecessor owns its own zero origin, separate from current ordinals. */
export class StreamPreviousOpening {
  private slot: {owner: string; batch: CursorPageBatch} | null = null
  matches(owner: StreamPreviousOwner) { return this.slot?.owner === previousOwnerKey(owner) }
  store(owner: StreamPreviousOwner, previous: {work_id: number; revision_id: string | null}, batch: CursorPageBatch): boolean {
    const first = batch.items[0]
    if (!first || batch.offset !== 0 || first.index !== 0 || first.page !== 0 ||
      first.work_id !== previous.work_id || (first.revision_id ?? null) !== previous.revision_id ||
      (owner.generation !== undefined && batch.generation !== owner.generation)) return false
    const items = batch.items.filter(page => sameStreamWork(page, first))
    if (items.length !== Math.min(STREAM_OPENING_PAGES, first.page_count) ||
      items.some((page, index) => page.index !== index || page.page !== index || !page.src || page.state !== 'verified' ||
        ['queued', 'waiting'].includes(page.recovery?.status || ''))) return false
    // The current owner follows the predecessor, so the new origin has a successor.
    this.slot = {owner: previousOwnerKey(owner), batch: {...batch, items: items.map(page => ({...page})), has_more: true}}
    return true
  }
  take(owner: StreamPreviousOwner): CursorPageBatch | null {
    const slot = this.slot; this.slot = null
    return slot?.owner === previousOwnerKey(owner) ? {...slot.batch, items: slot.batch.items.map(page => ({...page}))} : null
  }
  clear() { this.slot = null }
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
    if (this.distance < STREAM_WHEEL_DISTANCE) return 0
    this.consumed = true
    return direction
  }
  block(time: number) { this.last = time; this.eligible = false; this.consumed = true }
  reset() { this.last = -Infinity; this.eligible = false; this.consumed = false; this.distance = 0 }
}
