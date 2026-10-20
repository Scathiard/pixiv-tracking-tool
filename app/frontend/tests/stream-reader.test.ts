import {test} from 'node:test'
import assert from 'node:assert/strict'
import {streamPageWindow, streamTouchDirection, StreamPositions, StreamWheel} from '../src/state/streamReader.ts'
import type {PageData} from '../src/types.ts'

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
  assert.equal(streamTouchDirection(-63, false, true), 0)
  assert.equal(streamTouchDirection(-64, false, true), 1)
  assert.equal(streamTouchDirection(64, true, false), -1)
})
