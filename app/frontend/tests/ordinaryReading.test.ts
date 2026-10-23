import { test } from 'node:test'
import assert from 'node:assert/strict'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { ORDINARY_OPENING_PAGES, OrdinaryReadingCache, ordinaryNeighborPath, ordinaryOpeningWindow, type CursorNeighbors } from '../src/state/ordinaryReading.ts'
import type { WorkDetail } from '../src/types.ts'

const signal = () => new AbortController().signal
const neighbor = {id: 2, cursor: 'target-cursor', revision_id: 'revision-2'}
const source = {cursor: 'current-cursor', direction: 'next' as const}
function neighbors(generation = 7): CursorNeighbors { return {anchor_cursor: 'target-cursor', generation, previous: null, next: null} }
function opening(id: number, revision?: string | null): WorkDetail { return {id, revision_id: revision, pages: []} as unknown as WorkDetail }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return {promise, resolve}
}

test('a multi-page work displays its prepared small window once and later refreshes read live state', () => {
  const work = {...opening(2), display_page_count: 100,
    pages: Array.from({length: ORDINARY_OPENING_PAGES}, (_, index) => ({index, src: index ? null : '/synthetic/first', state: index ? 'missing' : 'verified'}))} as WorkDetail
  const take = ordinaryOpeningWindow(work)
  const first = take(0, 64, signal())!
  assert.equal(first.display_page_count, 100)
  assert.equal(first.pages.length, 4)
  assert.equal(first.pages[0]!.src, '/synthetic/first')
  assert.equal(take(0, 4, signal()), null, 'missing-media and forced refreshes must bypass the prepared response')
  assert.equal(take(4, 64, signal()), null, 'remaining pages use the existing extension path')
})

test('cancelled initial reads do not consume the window and a nonzero first read cannot reuse it later', () => {
  const work = {...opening(2), display_page_count: 10, pages: [{index: 0, src: '/synthetic/first', state: 'verified'}]} as WorkDetail
  const take = ordinaryOpeningWindow(work)
  const cancelled = new AbortController(); cancelled.abort()
  assert.throws(() => take(0, 64, cancelled.signal), {name: 'AbortError'})
  assert.equal(take(0, 1, signal())!.pages.length, 1)
  const resumed = ordinaryOpeningWindow(work)
  assert.equal(resumed(5, 64, signal()), null)
  assert.equal(resumed(0, 4, signal()), null)
})

test('foreground reuses the exact adjacent request and opening after a long work', async t => {
  let positions = 0, details = 0
  let now = 0
  t.mock.method(Date, 'now', () => now)
  const paths: string[] = []
  const cache = new OrdinaryReadingCache({
    neighbors: async path => { positions++; paths.push(path); return neighbors() },
    opening: async (id, revision) => { details++; return opening(id, revision) },
  })
  const query = 'tags=blue&tags=sky&favorites=1&ceiling=99&as_of=snapshot'
  await Promise.all([cache.readNeighbors(query, neighbor, 7, signal(), source), cache.readOpening(query, 2, 'revision-2', signal())])
  now = 60 * 60 * 1000
  await Promise.all([cache.readNeighbors(query, neighbor, 7, signal(), source), cache.readOpening(query, 2, 'revision-2', signal())])
  assert.equal(positions, 1); assert.equal(details, 1)
  assert.equal(paths[0], ordinaryNeighborPath(query, neighbor, source))
  const params = new URL(paths[0]!, 'http://synthetic.test').searchParams
  assert.deepEqual(params.getAll('tags'), ['blue', 'sky'])
  assert.equal(params.get('from_cursor'), source.cursor)
  assert.equal(params.get('from_direction'), 'next')
})

test('navigation takes ownership before cancelling prefetch, with no duplicate request', async () => {
  const position = deferred<CursorNeighbors>(), detail = deferred<WorkDetail>()
  let positions = 0, details = 0
  const requests: AbortSignal[] = []
  const cache = new OrdinaryReadingCache({
    neighbors: async (_path, owned) => { positions++; requests.push(owned); return position.promise },
    opening: async (_id, _revision, owned) => { details++; requests.push(owned); return detail.promise },
  })
  const prefetch = new AbortController(), foreground = new AbortController()
  const warming = Promise.all([
    cache.readNeighbors('sort=newest', neighbor, 7, prefetch.signal, source),
    cache.readOpening('sort=newest', 2, 'revision-2', prefetch.signal),
  ])
  const cancelled = assert.rejects(warming, {name: 'AbortError'})
  await nextTurn()
  const switching = Promise.all([
    cache.readNeighbors('sort=newest', neighbor, 7, foreground.signal, source),
    cache.readOpening('sort=newest', 2, 'revision-2', foreground.signal),
  ])
  prefetch.abort()
  await cancelled
  assert.ok(requests.every(owned => !owned.aborted))
  position.resolve(neighbors()); detail.resolve(opening(2, 'revision-2'))
  assert.equal((await switching)[1].id, 2)
  assert.equal(positions, 1); assert.equal(details, 1)
})

test('scope, work version, source cursor, and catalogue generation isolate cached reads', async () => {
  let positions = 0, details = 0
  const cache = new OrdinaryReadingCache({
    neighbors: async () => { positions++; return neighbors() },
    opening: async (id, revision) => { details++; return opening(id, revision) },
  })
  await cache.readNeighbors('favorites=1', neighbor, 7, signal(), source)
  await cache.readNeighbors('favorites=0', neighbor, 7, signal(), source)
  await cache.readNeighbors('favorites=1', {...neighbor, revision_id: 'changed'}, 7, signal(), source)
  await cache.readNeighbors('favorites=1', neighbor, 8, signal(), source)
  await cache.readNeighbors('favorites=1', neighbor, 8, signal(), {...source, cursor: 'changed'})
  await cache.readOpening('favorites=1', 2, 'revision-2', signal())
  await cache.readOpening('favorites=0', 2, 'revision-2', signal())
  await cache.readOpening('favorites=1', 2, 'changed', signal())
  assert.equal(positions, 5); assert.equal(details, 3)
})

test('invalidation separates new readers and late old responses cannot replace their cache', async () => {
  const old = deferred<CursorNeighbors>()
  let positions = 0
  const cache = new OrdinaryReadingCache({
    neighbors: async () => ++positions === 1 ? old.promise : neighbors(8),
    opening: async id => opening(id),
  })
  const pending = cache.readNeighbors('', neighbor, 7, signal(), source)
  await nextTurn()
  cache.clear()
  assert.equal((await cache.readNeighbors('', neighbor, 7, signal(), source)).generation, 8)
  old.resolve(neighbors(7)); await pending
  assert.equal((await cache.readNeighbors('', neighbor, 7, signal(), source)).generation, 8)
  assert.equal(positions, 2)
})

test('the local window remains bounded and cancelled rapid switches cannot populate it', async () => {
  let positions = 0, details = 0
  const late = deferred<WorkDetail>()
  const cache = new OrdinaryReadingCache({
    neighbors: async () => { positions++; return neighbors() },
    opening: async (id, revision) => { details++; return id === 9 ? late.promise : opening(id, revision) },
  })
  for (let id = 1; id <= 4; id++) {
    await cache.readNeighbors('', {id}, 7, signal())
    await cache.readOpening('', id, null, signal())
  }
  await cache.readNeighbors('', {id: 1}, 7, signal())
  await cache.readOpening('', 1, null, signal())
  assert.equal(positions, 5); assert.equal(details, 5)
  const cancelled = new AbortController()
  const pending = cache.readOpening('', 9, null, cancelled.signal)
  const rejected = assert.rejects(pending, {name: 'AbortError'})
  await nextTurn(); cancelled.abort(); await rejected
  late.resolve(opening(9)); await nextTurn()
  await cache.readOpening('', 9, null, signal())
  assert.equal(details, 7)
})
