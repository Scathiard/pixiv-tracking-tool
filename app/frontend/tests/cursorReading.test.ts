import { test } from 'node:test'
import assert from 'node:assert/strict'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { ApiReadError } from '../src/api.ts'
import { CursorReading, type CursorPageBatch, type CursorPageRequest } from '../src/state/cursorReading.ts'
import type { PageData } from '../src/types.ts'

const signal = () => new AbortController().signal
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return {promise, resolve}
}
function slot(index: number, workId: number, page: number, count: number, workIndex: number, generation = 7, previous: number | null = null): PageData {
  return {index, work_id: workId, page, page_count: count, work_index: workIndex, work_cursor: `work-${workId}:${generation}`,
    previous_work_index: previous, next_work_index: index - page + count, title: `Work ${workId}`, artist_name: 'Synthetic',
    artist_id: 1, pixiv_id: String(workId), type: count > 1 ? 'manga' : 'illust', x_restrict: null,
    src: `/synthetic/${workId}/${page}`, width: 10, height: 20, state: 'verified'}
}
function batch(items: PageData[], generation = 7, more = true, offset = items[0]?.index ?? 0): CursorPageBatch {
  return {items, generation, has_more: more, total: (items.at(-1)?.index ?? offset - 1) + 1 + Number(more), offset,
    ceiling: 999, as_of: `snapshot-${generation}`}
}
function fixture(counts: number[], generation = 7) {
  const calls: {params: URLSearchParams; signal: AbortSignal}[] = []
  const request: CursorPageRequest = async (path, options) => {
    const params = new URL(path, 'http://synthetic.test').searchParams
    calls.push({params, signal: options.signal})
    const limit = Number(params.get('limit')), backward = params.get('direction') === 'previous'
    const id = Number(params.get('anchor_work_id'))
    let work = id ? id - 1 : 0, ordinal = Number(params.get('work_index')), index = Number(params.get('index'))
    let page = id ? Number(params.get('page')) : 0
    if (params.has('cursor')) assert.equal(params.get('cursor'), `work-${id}:${generation}`)
    if (backward && page < 0) { work--; ordinal--; page = counts[work]! - 1 }
    if (!backward && page >= counts[work]!) { work++; ordinal++; page = 0 }
    const items: PageData[] = []
    for (; work >= 0 && work < counts.length && items.length < limit; backward ? work-- : work++) {
      const count = counts[work]!, start = index - page
      const previous = work ? start - counts[work - 1]! : null
      for (; page >= 0 && page < count && items.length < limit; backward ? page-- : page++) {
        items.push(slot(index, work + 1, page, count, ordinal, generation, previous))
        index += backward ? -1 : 1
      }
      if (items.length === limit) break
      ordinal += backward ? -1 : 1
      page = backward ? counts[work - 1]! - 1 : 0
    }
    if (backward) items.reverse()
    const last = items.at(-1)
    const more = backward ? !!items.length : !!last && (last.page + 1 < last.page_count || last.work_id < counts.length)
    return batch(items, generation, more, items[0]?.index ?? Number(params.get('index')))
  }
  return {request, calls}
}

test('opening and forward windows use identity tokens and retain the frozen filter scope', async () => {
  const server = fixture([2, 3, 1])
  const reading = new CursorReading('tags=blue&tags=sky&sort=oldest&page=90&index=900&cursor=old', server)
  const first = await reading.loader(0, 3, signal())
  assert.deepEqual(first.items.map(page => page.index), [0, 1, 2])
  assert.equal(first.total, 6)
  assert.equal(reading.generation, 7)
  assert.equal(server.calls[0]!.params.has('cursor'), false)
  assert.equal(server.calls[0]!.params.has('anchor_work_id'), false)
  assert.equal(server.calls[0]!.params.get('page'), '0')
  assert.equal(server.calls[0]!.params.get('index'), '0')
  assert.equal(server.calls[0]!.params.get('work_index'), '0')
  assert.deepEqual(server.calls[0]!.params.getAll('tags'), ['blue', 'sky'])
  assert.equal(server.calls[0]!.params.get('sort'), 'oldest')

  const middle = await reading.load(3, 2, signal())
  assert.deepEqual(middle.items.map(page => page.index), [3, 4])
  assert.equal(server.calls[1]!.params.get('anchor_work_id'), '2')
  assert.equal(server.calls[1]!.params.get('page'), '1')
  assert.equal(server.calls[1]!.params.get('index'), '3')
  assert.equal(server.calls[1]!.params.get('work_index'), '1')
  assert.equal(server.calls[1]!.params.get('cursor'), 'work-2:7')
  assert.equal(server.calls[1]!.params.get('ceiling'), '999')
  assert.equal(server.calls[1]!.params.get('as_of'), 'snapshot-7')

  const end = await reading.load(5, 40, signal())
  assert.deepEqual(end.items.map(page => page.index), [5])
  assert.equal(end.total, 6)
  assert.equal(end.has_more, false)
  assert.deepEqual(reading.locate(2), {work_id: 2, page: 0, index: 2, work_index: 1, offset: 0})
})

test('a long work can be skipped using its span without loading its middle', async () => {
  const server = fixture([2000, 2])
  const reading = new CursorReading('', server)
  const opening = await reading.load(0, 4, signal())
  assert.equal(opening.total, 2001)
  const next = await reading.load(1984, 64, signal())
  assert.equal(server.calls.length, 2)
  assert.equal(server.calls[1]!.params.get('anchor_work_id'), '1')
  assert.equal(server.calls[1]!.params.get('page'), '1984')
  assert.equal(server.calls[1]!.params.get('index'), '1984')
  assert.equal(next.items.find(page => page.index === 2000)?.work_id, 2)
  assert.equal(next.total, 2002)
  assert.equal(reading.retainedPages, 22)
})

test('the next-work endpoint itself remains usable after a long work opening', async () => {
  const server = fixture([2000, 3])
  const reading = new CursorReading('', server)
  await reading.load(0, 1, signal())
  const result = await reading.load(2000, 2, signal())
  assert.equal(server.calls[1]!.params.get('page'), '2000')
  assert.equal(result.items[0]!.work_id, 2)
  assert.equal(result.items[0]!.work_index, 1)
})

test('backward extension from an evicted boundary returns canonical ascending pages', async () => {
  const server = fixture(Array.from({length: 400}, () => 1))
  const reading = new CursorReading('', server)
  for (let offset = 0; offset < 300; offset += 60) await reading.load(offset, 60, signal())
  assert.equal(reading.retainedPages, 240)
  assert.equal(reading.anchorAt(59), null)
  const previous = await reading.load(20, 40, signal())
  const request = server.calls.at(-1)!.params
  assert.equal(request.get('direction'), 'previous')
  assert.equal(request.get('anchor_work_id'), '61')
  assert.equal(request.get('page'), '-1')
  assert.equal(request.get('index'), '59')
  assert.deepEqual(previous.items.map(page => page.index), Array.from({length: 40}, (_, index) => index + 20))
  assert.equal(previous.total, 301)
  assert.equal(reading.retainedPages, 240)
})

test('a backward read preserves an exact forward end instead of reporting its own window end', async () => {
  const server = fixture([2, 3, 5])
  const reading = new CursorReading('', {...server, maxPages: 4})
  await reading.load(0, 4, signal())
  const end = await reading.load(4, 120, signal())
  assert.equal(end.total, 10)
  assert.equal(end.has_more, false)
  const previous = await reading.load(2, 4, signal())
  assert.deepEqual(previous.items.map(page => page.index), [2, 3, 4, 5])
  assert.equal(previous.total, 10)
})

test('an evicted long predecessor needs just one probe and one anchored window', async () => {
  const server = fixture([30, 2000, 2000])
  const reading = new CursorReading('q=synthetic', server)
  reading.seed(batch([slot(2030, 3, 0, 2000, 2, 7, 30), slot(2031, 3, 1, 2000, 2, 7, 30)]))
  const previous = await reading.load(14, 64, signal())
  assert.equal(server.calls.length, 2)
  const probe = server.calls[0]!.params, window = server.calls[1]!.params
  assert.equal(probe.get('limit'), '1')
  assert.equal(probe.get('anchor_work_id'), '3')
  assert.equal(probe.get('page'), '-1')
  assert.equal(probe.get('index'), '2029')
  assert.equal(window.get('direction'), 'previous')
  assert.equal(window.get('anchor_work_id'), '2')
  assert.equal(window.get('cursor'), 'work-2:7')
  assert.equal(window.get('page'), '47')
  assert.equal(window.get('index'), '77')
  assert.deepEqual(previous.items.map(page => page.index), Array.from({length: 64}, (_, index) => index + 14))
  assert.equal(previous.items.find(page => page.index === 30)?.page, 0)
  assert.equal(previous.total, 4031)
  assert.equal(reading.anchorAt(2029), null, 'the distant probe is not retained as traversal history')
})

test('numeric jumps beyond current and adjacent works fail without scanning history', async () => {
  const server = fixture(Array.from({length: 1000}, () => 1))
  const reading = new CursorReading('', {...server, maxPages: 4})
  await assert.rejects(reading.load(500, 10, signal()), /只能从已载入的作品继续/)
  assert.equal(server.calls.length, 0)
  reading.seed(batch([slot(500, 501, 0, 1, 500, 7, 499)]))
  await assert.rejects(reading.load(100, 64, signal()), /重新定位作品/)
  assert.equal(server.calls.length, 0)
})

test('continued traversal retains at most 240 pages and never builds a full visited manifest', async () => {
  const server = fixture(Array.from({length: 1500}, () => 1))
  const reading = new CursorReading('', {...server, maxPages: 10000})
  for (let offset = 0; offset < 1200; offset += 40) {
    await reading.load(offset, 40, signal())
    assert.ok(reading.retainedPages <= 240)
  }
  assert.equal(reading.retainedPages, 240)
  assert.equal(reading.anchorAt(0), null)
  assert.equal(reading.anchorAt(959), null)
  assert.equal(reading.anchorAt(960)?.work_id, 961)
  assert.equal(reading.locate(1), null)
  assert.equal(server.calls.length, 30)
})

test('each individual network read is bounded to 120 pages', async () => {
  const server = fixture([2000])
  const reading = new CursorReading('', server)
  const result = await reading.load(0, 10000, signal())
  assert.equal(server.calls[0]!.params.get('limit'), '120')
  assert.equal(result.items.length, 120)
})

test('an aborted late response cannot install anchors or its generation', async () => {
  const pending = deferred<CursorPageBatch>()
  const reading = new CursorReading('', {request: () => pending.promise})
  const controller = new AbortController()
  const loading = reading.load(0, 2, controller.signal)
  const rejected = assert.rejects(loading, {name: 'AbortError'})
  controller.abort()
  await rejected
  pending.resolve(batch([slot(0, 1, 0, 2, 0, 99)], 99))
  await nextTurn()
  assert.equal(reading.generation, undefined)
  assert.equal(reading.retainedPages, 0)
})

test('a superseded request cannot overwrite a newer successful window', async () => {
  const pending = deferred<CursorPageBatch>()
  let count = 0
  const reading = new CursorReading('', {request: () => ++count === 1 ? pending.promise : Promise.resolve(batch([slot(0, 1, 0, 2, 0, 11)], 11))})
  const first = reading.load(0, 1, signal())
  const rejected = assert.rejects(first, {name: 'AbortError'})
  await reading.load(0, 1, signal())
  await rejected
  pending.resolve(batch([slot(0, 1, 0, 2, 0, 10)], 10))
  await nextTurn()
  assert.equal(reading.generation, 11)
  assert.equal(reading.anchorAt(0)?.work_cursor, 'work-1:11')
  assert.equal(reading.retainedPages, 1)
})

test('generation mismatch refuses to combine windows and leaves recovery to the caller', async () => {
  const opening = fixture([2, 3], 7)
  let calls = 0
  const reading = new CursorReading('', {request: async (...args) => {
    const result = await opening.request(...args)
    return ++calls === 1 ? result : {...result, generation: 8, items: result.items.map(page => ({...page, work_cursor: `work-${page.work_id}:8`}))}
  }})
  await reading.load(0, 1, signal())
  await assert.rejects(reading.load(1, 1, signal()), (error: unknown) => {
    assert.ok(error instanceof ApiReadError)
    assert.equal(error.code, 'cursor_expired')
    return true
  })
  assert.equal(reading.generation, 7)
  assert.equal(reading.retainedPages, 1)
  assert.equal(reading.anchorAt(1), null)
  assert.equal(reading.anchorAt(0)?.work_cursor, 'work-1:7')
})

test('an expiration error propagates unchanged without automatic restart', async () => {
  const error = new ApiReadError('Snapshot expired', 'cursor_expired')
  const reading = new CursorReading('', {request: async () => { throw error }})
  await assert.rejects(reading.load(0, 1, signal()), caught => caught === error)
  assert.equal(reading.generation, undefined)
  assert.equal(reading.retainedPages, 0)
})

test('recovery retains selected identity and original snapshot parameters while removing its token', async () => {
  const fresh = fixture([2, 3], 8)
  const reading = new CursorReading('q=synthetic&tags=blue&ceiling=5&as_of=original&page=20', fresh)
  reading.seed(batch([slot(3, 2, 1, 3, 1, 7, 0)]))
  reading.recover(reading.anchorAt(3))
  assert.equal(reading.generation, undefined)
  assert.equal(reading.retainedPages, 1)
  assert.equal(reading.anchorAt(3)?.work_cursor, undefined)
  const result = await reading.load(3, 1, signal())
  const request = fresh.calls[0]!.params
  assert.equal(request.get('anchor_work_id'), '2')
  assert.equal(request.get('page'), '1')
  assert.equal(request.get('index'), '3')
  assert.equal(request.get('work_index'), '1')
  assert.equal(request.has('cursor'), false)
  assert.equal(request.get('ceiling'), '999')
  assert.equal(request.get('as_of'), 'snapshot-7')
  assert.equal(request.get('q'), 'synthetic')
  assert.equal(request.get('tags'), 'blue')
  assert.equal(result.items[0]!.work_id, 2)
  assert.equal(reading.generation, 8)
})

test('reset aborts pending work and a late old response cannot resurrect the old visit', async () => {
  const pending = deferred<CursorPageBatch>()
  const reading = new CursorReading('', {request: () => pending.promise})
  reading.seed(batch([slot(0, 1, 0, 2, 0)]))
  const loading = reading.load(1, 1, signal())
  const rejected = assert.rejects(loading, {name: 'AbortError'})
  reading.reset()
  await rejected
  pending.resolve(batch([slot(1, 1, 1, 2, 0)]))
  await nextTurn()
  assert.equal(reading.generation, undefined)
  assert.equal(reading.retainedPages, 0)
})

test('abort between predecessor probe and final read keeps the whole operation uninstalled', async () => {
  const server = fixture([30, 2000, 2000]), pending = deferred<CursorPageBatch>()
  let calls = 0
  const reading = new CursorReading('', {request: (...args) => ++calls === 1 ? server.request(...args) : pending.promise})
  reading.seed(batch([slot(2030, 3, 0, 2000, 2, 7, 30)]))
  const controller = new AbortController()
  const loading = reading.load(14, 64, controller.signal)
  const rejected = assert.rejects(loading, {name: 'AbortError'})
  await nextTurn()
  assert.equal(calls, 2)
  controller.abort()
  await rejected
  pending.resolve(batch([slot(14, 1, 14, 30, 0)]))
  await nextTurn()
  assert.equal(reading.retainedPages, 1)
  assert.equal(reading.anchorAt(2030)?.work_id, 3)
  assert.equal(reading.anchorAt(2029), null)
})

test('a generation change between probe and final read cannot install either batch', async () => {
  const server = fixture([30, 2000, 2000])
  let calls = 0
  const reading = new CursorReading('', {request: async (...args) => {
    const result = await server.request(...args)
    return ++calls === 1 ? result : {...result, generation: 8}
  }})
  reading.seed(batch([slot(2030, 3, 0, 2000, 2, 7, 30)]))
  await assert.rejects(reading.load(14, 64, signal()), (error: unknown) => error instanceof ApiReadError && error.code === 'cursor_expired')
  assert.equal(reading.generation, 7)
  assert.equal(reading.retainedPages, 1)
})

test('known identity anchors still refresh page payloads instead of serving a private media cache', async () => {
  const server = fixture([3])
  let calls = 0
  const reading = new CursorReading('', {request: async (...args) => {
    const result = await server.request(...args)
    if (++calls > 1) result.items[0]!.src = '/synthetic/repaired'
    return result
  }})
  await reading.load(0, 1, signal())
  const refresh = await reading.load(0, 1, signal())
  assert.equal(server.calls.length, 2)
  assert.equal(refresh.items[0]!.src, '/synthetic/repaired')
  assert.equal(reading.anchorAt(0)?.src, '/synthetic/repaired')
})

test('invalid range and noncanonical responses do not poison the registry', async () => {
  let calls = 0
  const reading = new CursorReading('', {request: async () => { calls++; return batch([slot(1, 1, 1, 3, 0), slot(0, 1, 0, 3, 0)]) }})
  for (const offset of [-1, 0.5, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    await assert.rejects(reading.load(offset, 40, signal()), /范围无效/)
  }
  await assert.rejects(reading.load(0, 0, signal()), /范围无效/)
  assert.equal(calls, 0)
  await assert.rejects(reading.load(0, 2, signal()), /响应顺序无效/)
  assert.equal(reading.generation, undefined)
  assert.equal(reading.retainedPages, 0)
})


test('validated continuations survive repeated unrelated generation mutations', async () => {
  const opening = fixture([2, 3], 7)
  let generation = 7
  const reading = new CursorReading('q=synthetic&ceiling=999&as_of=frozen', {request: async (...args) => {
    const result = await opening.request(...args)
    return {...result, generation: generation++, continuation_rebased: true}
  }})
  await reading.load(0, 1, signal())
  await reading.load(1, 1, signal())
  await reading.load(2, 1, signal())
  assert.equal(reading.generation, 9)
  assert.equal(reading.anchorAt(2)?.work_id, 2)
  assert.equal(opening.calls.length, 3)
})

test('expired continuation retries the same identity, direction and frozen scope only once', async () => {
  const server = fixture([2, 3], 8), requests: URLSearchParams[] = []
  const reading = new CursorReading('tags=blue&sort=oldest&ceiling=999&as_of=frozen', {request: async (path, options) => {
    const params = new URL(path, 'http://synthetic.test').searchParams
    requests.push(params)
    if (params.has('cursor')) throw new ApiReadError('changed boundary', 'cursor_expired')
    return server.request(path, options)
  }})
  reading.seed(batch([slot(1, 1, 1, 2, 0)]))
  const result = await reading.load(2, 1, signal())
  assert.equal(result.items[0]!.work_id, 2)
  assert.equal(reading.generation, 8)
  assert.equal(requests.length, 2)
  assert.equal(requests[1]!.has('cursor'), false)
  for (const field of ['anchor_work_id', 'page', 'index', 'work_index', 'direction', 'ceiling', 'as_of', 'tags', 'sort']) {
    assert.equal(requests[1]!.get(field), requests[0]!.get(field), field)
  }
  const removed = new CursorReading('', {request: async () => { throw new ApiReadError('removed', 'cursor_expired') }})
  removed.seed(batch([slot(0, 1, 0, 1, 0)]))
  await assert.rejects(removed.load(1, 1, signal()), /removed/)
  assert.equal(removed.generation, 7)
})
