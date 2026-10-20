import {test} from 'node:test'
import assert from 'node:assert/strict'
import {streamPageWindow, streamIdentity, streamDragOffset, streamCanCommit, StreamWheelDrag} from '../src/state/streamReader.ts'
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

test('two single-page random works cannot merge their next-round repetitions into this panel', () => {
  const items = [page(0, {page: 0, page_count: 1, round: 0}), page(1, {work_id: 9, page: 0, page_count: 1, round: 0}),
    page(2, {page: 0, page_count: 1, round: 1}), page(3, {work_id: 9, page: 0, page_count: 1, round: 1})]
  assert.deepEqual(streamPageWindow(items, items[0]!).map(item => item.index), [0])
  assert.deepEqual(streamPageWindow(items, items[2]!).map(item => item.index), [2])
  assert.deepEqual(streamPageWindow(items, {...items[0]!, round: undefined}).map(item => item.index), [0])
})
test('track follows the whole viewport and only ready uncancelled releases can commit', () => {
  assert.equal(streamDragOffset(-340, 600), -340)
  assert.equal(streamDragOffset(340, 600), 340)
  assert.ok(streamDragOffset(-900, 600) > -900, 'overtravel is resisted')
  assert.equal(streamCanCommit(-119, true), false)
  assert.equal(streamCanCommit(-120, true), true)
  assert.equal(streamCanCommit(-300, false), false, 'late preparation cannot authorize a released drag')
  assert.equal(streamCanCommit(-300, true, true), false)
  assert.notEqual(streamIdentity(page(100, {round: 0})), streamIdentity(page(100, {round: 1})))
})

test('wheel displacement cannot be armed by momentum that only reaches the work edge', () => {
  const wheel = new StreamWheelDrag()
  let boundary = false
  const edge = () => boundary
  assert.equal(wheel.feed(80, 0, edge), 0)
  boundary = true
  assert.equal(wheel.feed(80, 100, edge), 0)
  assert.equal(wheel.feed(40, 400, edge), -40)
  assert.equal(wheel.feed(50, 450, edge), -90)
  wheel.block(460)
  assert.equal(wheel.feed(100, 500, edge), 0)
  assert.equal(wheel.feed(-80, 900, edge), 80)
})
