import { api, ApiReadError, url } from '../api.ts'
import type { Anchor, PageBatch, PageData, PageLoader } from '../types.ts'

export interface CursorPageBatch extends PageBatch {
  generation: number
  has_more: boolean
}
export type CursorPageRequest = (path: string, options: {signal: AbortSignal}) => Promise<CursorPageBatch>
export interface CursorReadingOptions {request?: CursorPageRequest; maxPages?: number}

const MAX_PAGES = 240, MAX_READ = 120
const TRANSPORT_PARAMETERS = ['cursor', 'direction', 'anchor_work_id', 'limit', 'offset', 'page', 'page_offset', 'index', 'work_index', 'mode']
interface WindowRequest {direction: 'next' | 'previous'; page?: PageData; index: number; pageIndex: number; limit?: number}

/** Browser-local page ordinals over bounded identity/keyset windows, without a visit manifest. */
export class CursorReading {
  private readonly query: URLSearchParams
  private readonly request: CursorPageRequest
  private readonly maximum: number
  private pages = new Map<number, PageData>()
  private revision = 0
  private controller: AbortController | null = null
  private currentGeneration: number | undefined
  private extent = 0
  private finalExtent: number | undefined

  readonly loader: PageLoader = (offset, limit, signal) => this.load(offset, limit, signal)

  constructor(query: string, options: CursorReadingOptions = {}) {
    this.query = new URLSearchParams(query)
    for (const name of TRANSPORT_PARAMETERS) this.query.delete(name)
    this.request = options.request || ((path, settings) => api<CursorPageBatch>(path, settings))
    this.maximum = Math.min(MAX_PAGES, Math.max(1, Number.isSafeInteger(options.maxPages) ? options.maxPages! : MAX_PAGES))
  }

  get generation() { return this.currentGeneration }
  get retainedPages() { return this.pages.size }

  anchorAt(index: number): PageData | null {
    const page = this.pages.get(index)
    return page ? {...page} : null
  }

  locate(workId: number): Anchor | null {
    const page = [...this.pages.values()].find(page => page.work_id === workId)
    return page ? {work_id: workId, page: 0, index: page.index - page.page, work_index: page.work_index, offset: 0} : null
  }

  /** Retain only recent identity anchors. Image payloads are still read from the server. */
  remember(pages: readonly PageData[]) {
    for (const page of pages) this.validatePage(page)
    for (const page of pages) {
      this.pages.delete(page.index)
      this.pages.set(page.index, {...page})
      while (this.pages.size > this.maximum) this.pages.delete(this.pages.keys().next().value!)
    }
  }

  seed(batch: CursorPageBatch) {
    this.validateBatch(batch, MAX_READ)
    this.checkGeneration(batch.generation)
    this.invalidate()
    this.install(batch, 'next')
  }

  reset() {
    this.invalidate()
    this.pages.clear()
    this.currentGeneration = undefined
    this.extent = 0; this.finalExtent = undefined
    this.query.delete('ceiling'); this.query.delete('as_of')
  }

  /** Drop expired tokens and the old snapshot, keeping just the selected identity. */
  recover(page?: PageData | null) {
    if (page) this.validatePage(page)
    this.reset()
    if (page) this.remember([{...page, work_cursor: undefined}])
  }

  async load(offset: number, limit: number, signal: AbortSignal): Promise<CursorPageBatch> {
    signal.throwIfAborted()
    if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1) {
      throw new Error('图片页读取范围无效，请重新定位')
    }
    limit = Math.min(MAX_READ, limit)
    let selected = this.select(offset, limit)
    const predecessor = selected ? null : this.predecessor(offset, limit)
    if (!selected && !predecessor) throw this.unsupportedJump()
    this.invalidate()
    const revision = this.revision
    const controller = this.controller = new AbortController()
    const ownedSignal = AbortSignal.any([signal, controller.signal])
    try {
      let probe: CursorPageBatch | undefined
      if (predecessor) {
        // One page supplies the adjacent work's identity and span, even after
        // its old window was evicted. It is never installed on its own.
        probe = await untilAborted(this.request(this.path(predecessor, 1), {signal: ownedSignal}), ownedSignal)
        ownedSignal.throwIfAborted()
        this.validateBatch(probe, 1)
        this.checkGeneration(probe.generation)
        if (probe.items.length !== 1 || probe.items[0]!.index !== predecessor.index) {
          throw new ApiReadError('阅读范围已变化，请重新定位后继续', 'cursor_expired')
        }
        selected = this.select(offset, limit, probe.items)
        if (!selected) throw this.unsupportedJump()
      }
      const batch = await untilAborted(this.request(this.path(selected!, selected!.limit ?? limit, probe), {signal: ownedSignal}), ownedSignal)
      ownedSignal.throwIfAborted()
      if (revision !== this.revision) throw new DOMException('图片页读取已被替换', 'AbortError')
      this.validateBatch(batch, selected!.limit ?? limit)
      this.checkGeneration(batch.generation)
      if (probe && batch.generation !== probe.generation) {
        throw new ApiReadError('阅读范围已变化，请重新定位后继续', 'cursor_expired')
      }
      if (batch.items.length && (batch.items[0]!.index !== offset ||
          selected!.direction === 'previous' && batch.items.at(-1)!.index !== selected!.index)) {
        throw new ApiReadError('阅读范围已变化，请重新定位后继续', 'cursor_expired')
      }
      this.install(batch, selected!.direction)
      return {...batch, total: this.extent}
    } finally {
      if (this.controller === controller) this.controller = null
    }
  }

  private invalidate() {
    this.revision++
    this.controller?.abort()
    this.controller = null
  }

  private select(offset: number, limit: number, pages: readonly PageData[] = [...this.pages.values()]): WindowRequest | null {
    const exact = pages.find(page => page.index === offset)
    if (exact) return {direction: 'next', page: exact, index: offset, pageIndex: exact.page}
    // A long work's end is known from its small page window, so skipping its
    // unvisited middle does not require storing every intervening page.
    for (const page of pages) {
      const start = page.index - page.page, end = start + page.page_count
      if (offset >= start && offset <= end) {
        return {direction: 'next', page, index: offset, pageIndex: offset - start}
      }
    }
    // PageReader includes a little context before a work jump. Anchor at the
    // known work's endpoint and read backwards to include that context.
    for (const page of pages) {
      const start = page.index - page.page, end = start + page.page_count
      if (offset < start && offset + limit > start) {
        const endpoint = Math.min(offset + limit - 1, end - 1)
        return {direction: 'previous', page, index: endpoint, pageIndex: endpoint - start, limit: endpoint - offset + 1}
      }
    }
    const first = [...pages].sort((left, right) => left.index - right.index)[0]
    if (first && offset + limit === first.index && first.index > 0) {
      return {direction: 'previous', page: first, index: first.index - 1, pageIndex: first.page - 1}
    }
    if (offset === 0) return {direction: 'next', index: 0, pageIndex: 0}
    return null
  }

  private predecessor(offset: number, limit: number): WindowRequest | null {
    for (const page of this.pages.values()) {
      const start = page.index - page.page, previous = page.previous_work_index
      if (Number.isSafeInteger(previous) && previous! >= 0 && previous! < start &&
          offset < start && offset + limit > previous!) {
        return {direction: 'previous', page, index: start - 1, pageIndex: -1}
      }
    }
    return null
  }

  private unsupportedJump() {
    return new Error('顺序阅览只能从已载入的作品继续；请按上一件、下一件或重新定位作品')
  }

  private path(selected: WindowRequest, limit: number, snapshot?: CursorPageBatch) {
    const extras: Record<string, string | number> = {limit, direction: selected.direction,
      page: selected.pageIndex, index: selected.index, work_index: selected.page?.work_index ?? 0}
    if (selected.page) {
      extras.anchor_work_id = selected.page.work_id
      if (selected.page.work_cursor) extras.cursor = selected.page.work_cursor
    }
    if (snapshot?.ceiling !== undefined) extras.ceiling = snapshot.ceiling
    if (snapshot?.as_of) extras.as_of = snapshot.as_of
    return url('/api/library/cursor/feed', this.query.toString(), extras)
  }

  private checkGeneration(generation: number) {
    if (this.currentGeneration !== undefined && generation !== this.currentGeneration) {
      throw new ApiReadError('图库已变化，当前位置仍保留；重新定位后可继续', 'cursor_expired')
    }
  }

  private install(batch: CursorPageBatch, direction: 'next' | 'previous') {
    this.currentGeneration = batch.generation
    if (batch.ceiling !== undefined) this.query.set('ceiling', String(batch.ceiling))
    if (batch.as_of) this.query.set('as_of', batch.as_of)
    const end = batch.items.at(-1)?.index !== undefined ? batch.items.at(-1)!.index + 1 : batch.offset
    if (direction === 'next' && !batch.has_more) this.finalExtent = end
    const provisional = Math.max(end + (batch.has_more ? 1 : 0),
      ...batch.items.map(page => page.next_work_index + 1))
    this.extent = this.finalExtent ?? Math.max(this.extent, provisional)
    this.remember(batch.items)
  }

  private validatePage(page: PageData) {
    if (![page.index, page.work_index, page.page, page.page_count, page.next_work_index].every(Number.isSafeInteger) ||
        page.index < 0 || page.work_index < 0 || page.page < 0 || page.page_count < 1 || page.page >= page.page_count ||
        page.index < page.page || page.next_work_index !== page.index - page.page + page.page_count ||
        !Number.isSafeInteger(page.work_id) || page.work_id < 1) {
      throw new Error('图片页定位信息无效，请重新打开当前范围')
    }
  }

  private validateBatch(batch: CursorPageBatch, limit: number) {
    if (!Number.isSafeInteger(batch.generation) || batch.generation < 0 || typeof batch.has_more !== 'boolean' ||
        !Number.isSafeInteger(batch.offset) || batch.offset < 0 || !Array.isArray(batch.items) || batch.items.length > limit) {
      throw new Error('图片页游标响应无效，请重新打开当前范围')
    }
    for (let index = 0; index < batch.items.length; index++) {
      const page = batch.items[index]!
      this.validatePage(page)
      if (!page.work_cursor || page.index !== batch.offset + index) {
        throw new Error('图片页游标响应顺序无效，请重新打开当前范围')
      }
    }
  }
}

function untilAborted<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason) }
    signal.addEventListener('abort', abort, {once: true})
    promise.then(value => { signal.removeEventListener('abort', abort); resolve(value) }, error => {
      signal.removeEventListener('abort', abort); reject(error)
    })
    if (signal.aborted) abort()
  })
}
