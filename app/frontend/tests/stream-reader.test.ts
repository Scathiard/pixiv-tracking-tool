import {test} from 'node:test'
import assert from 'node:assert/strict'
import {streamPageWindow, streamTouchDirection, StreamPositions, StreamWheel, StreamOpenings, StreamPreviousOpening} from '../src/state/streamReader.ts'
import type {PageBatch, PageData} from '../src/types.ts'

const page = (index: number, changes: Partial<PageData> = {}) => ({index, work_id: 8, work_index: 3, revision_id: 'one', page: index - 100, page_count: 2000, ...changes} as PageData)

test('a 2000-page work retains a bounded revision window and true global indices', () => {
  const items = Array.from({length: 2000}, (_, index) => page(index + 100))
  const result = streamPageWindow([...items, page(2050, {work_id: 9}), page(1550, {revision_id: 'two'})], items[1450]!)
  assert.equal(result.length, 120)
  assert.ok(result.every(candidate => candidate.work_id === 8 && candidate.revision_id === 'one'))
  assert.ok(result.some(candidate => candidate.index === 1550 && candidate.page === 1450))
  assert.equal(result.at(-1)!.page < 1999, true, 'window end is not the work end')
})
test('extension evicts opposite metadata without allowing mixed works', () => {
  const items = Array.from({length: 160}, (_, index) => page(index + 100))
  assert.equal(streamPageWindow(items, items[0]!, 1)[0]!.index, 140)
  assert.equal(streamPageWindow(items, items[0]!, -1).at(-1)!.index, 219)
})
test('positions are visit-only and rebased to the current work identity', () => {
  const positions = new StreamPositions(), original = page(140)
  positions.remember(original, {work_id: 8, work_index: 3, page: 40, index: 140, offset: 215})
  assert.deepEqual(positions.read(page(0, {page: 0, work_index: 0})), {work_id: 8, work_index: 0, page: 40, index: 40, offset: 215})
  assert.equal(positions.read(page(140, {revision_id: 'two'})), null)
  positions.clear(); assert.equal(positions.read(original), null)
})
test('momentum reaching an edge cannot switch a work until a new wheel burst', () => {
  const wheel = new StreamWheel()
  let atEnd = false
  const edge = (direction: number) => direction > 0 && atEnd
  assert.equal(wheel.feed(100, 0, edge), 0)
  atEnd = true
  assert.equal(wheel.feed(100, 90, edge), 0)
  assert.equal(wheel.feed(100, 150, edge), 0)
  assert.equal(wheel.feed(40, 400, edge), 0)
  assert.equal(wheel.feed(30, 410, edge), 1)
  assert.equal(wheel.feed(100, 430, edge), 0)
})
test('zoom-owned wheel remainder cannot become a new boundary gesture', () => {
  const wheel = new StreamWheel(), edge = () => true
  wheel.block(100)
  assert.equal(wheel.feed(100, 150, edge), 0)
  assert.equal(wheel.feed(100, 390, edge), 1)
})
test('a swipe must start at the corresponding true edge before it can switch works', () => {
  assert.equal(streamTouchDirection(-200, false, false), 0)
  assert.equal(streamTouchDirection(200, false, false), 0)
  assert.equal(streamTouchDirection(-200, true, false), 0)
  assert.equal(streamTouchDirection(200, false, true), 0)
  assert.equal(streamTouchDirection(-119, false, true), 0)
  assert.equal(streamTouchDirection(-120, false, true), 1)
  assert.equal(streamTouchDirection(120, true, false), -1)
})

test('nearby openings are bounded and cannot reuse a different snapshot or failed media', () => {
  const cache = new StreamOpenings()
  for (let work = 0; work < 8; work++) {
    const items = Array.from({length: 90}, (_, index) => page(work * 2000 + index, {
      work_id: work + 1, page: index, src: '/synthetic', state: 'verified',
    }))
    cache.remember({items, total: 20000, offset: items[0]!.index, catalogue_revision: 5}, items[0]!)
  }
  assert.equal(cache.size, 5)
  assert.equal(cache.retainedPages, 20)
  assert.equal(cache.read(0, 5), null)
  assert.equal(cache.read(14000, 4), null)
  const ready = cache.read(14000, 5)!
  assert.equal(ready.items.length, 4)
  ready.items[0]!.src = null
  assert.equal(cache.read(14000, 5)!.items[0]!.src, '/synthetic', 'callers cannot mutate retained metadata')
  cache.remember({...ready, items: [{...ready.items[0]!, src: null, state: 'missing'}]} as PageBatch, ready.items[0]!)
  assert.equal(cache.read(14000, 5), null, 'missing pages must keep the real refresh path')
  cache.clear(); assert.equal(cache.retainedPages, 0)
})

test('an identity predecessor keeps its own zero origin and is consumed only by its exact owner', () => {
  const slot = new StreamPreviousOpening()
  const owner = {sessionKey: 'feed:one', scope: 'tags=blue', workId: 8, revisionId: 'current', cursor: 'eight', generation: 5}
  const previous = {work_id: 7, revision_id: 'previous'}
  const items = Array.from({length: 4}, (_, index) => page(index, {work_id: 7, revision_id: 'previous',
    page: index, page_count: 2000, work_index: 0, next_work_index: 2000, src: '/synthetic', state: 'verified'}))
  const batch = {items, total: 2001, offset: 0, generation: 5, has_more: true}
  assert.equal(slot.store(owner, previous, batch), true)
  assert.equal(slot.matches({...owner, scope: 'tags=red'}), false)
  assert.equal(slot.matches({...owner, sessionKey: 'feed:two'}), false)
  assert.equal(slot.matches({...owner, revisionId: 'new'}), false)
  assert.equal(slot.take({...owner, generation: 6}), null)
  assert.equal(slot.store(owner, previous, {...batch, generation: 6}), false)
  assert.equal(slot.store(owner, previous, {...batch, items: [{...items[0]!, src: null}]}), false)
  assert.equal(slot.store(owner, previous, batch), true)
  assert.deepEqual(slot.take(owner)?.items.map(page => page.index), [0, 1, 2, 3], 'the predecessor is independently rebased, never assigned negative ordinals')
  assert.equal(slot.take(owner), null, 'the opening is transferred only once')
})
