import {test} from 'node:test'
import assert from 'node:assert/strict'
import {needsIdentityPrevious, RandomReadingPreparation, expiredReadingVisit, type ReadingVisit} from '../src/state/reading.ts'
import type {PageData} from '../src/types.ts'
const page = (changes: Partial<PageData>) => ({index: 2, page: 2, previous_work_index: 0, ...changes} as PageData)
test('identity-rebased previous navigation resolves a distinct neighbor instead of current index zero', () => {
  assert.equal(needsIdentityPrevious(page({})), true)
  assert.equal(needsIdentityPrevious(page({index: 0, page: 0})), true)
  assert.equal(needsIdentityPrevious(page({index: 202, page: 2})), false)
  assert.equal(needsIdentityPrevious(page({previous_work_index: null})), false)
})

test('random preparation creates one scope visit and materializes its shuffle before takeover', async () => {
  const calls: string[] = [], released: string[] = []
  const visit = {session_id: 'one', total: 3, page_total: 9}
  const preparation = new RandomReadingPreparation({
    start: async query => { calls.push(`start:${query}`); return visit },
    pages: async (_visit, order, offset, limit) => { calls.push(`${order}:${offset}:${limit}`); return {items: [], total: 9, offset} },
    release: item => { if (item) released.push(item.session_id) },
  })
  await Promise.all([preparation.warm('tags=blue'), preparation.warm('tags=blue')])
  assert.deepEqual(calls, ['start:tags=blue', 'random:0:1'])
  preparation.pause()
  assert.equal(preparation.peek('tags=blue'), visit, 'foreground navigation retains completed preparation')
  assert.equal(await preparation.take('tags=blue', new AbortController().signal), visit)
  preparation.clear()
  assert.deepEqual(released, [], 'the caller owns the claimed active visit')
})

test('superseded random preparation releases a late visit and cannot install it in a new scope', async () => {
  let finish!: (visit: ReadingVisit) => void
  const released: string[] = []
  const preparation = new RandomReadingPreparation({
    start: query => query === 'old' ? new Promise(resolve => { finish = resolve }) : Promise.resolve({session_id: 'new', total: 1, page_total: 1}),
    pages: async () => ({items: [], total: 1, offset: 0}),
    release: visit => { if (visit) released.push(visit.session_id) },
  })
  const old = preparation.warm('old')
  const rejected = assert.rejects(old)
  await preparation.warm('new')
  finish({session_id: 'old', total: 1, page_total: 1})
  await rejected
  assert.deepEqual(released, ['old'])
  assert.equal(preparation.peek('new')!.session_id, 'new')
  preparation.clear(); assert.deepEqual(released, ['old', 'new'])
  assert.equal(expiredReadingVisit(new Error('阅读范围已过期，请重新打开图库；本地作品未改变')), true)
  assert.equal(expiredReadingVisit(new Error('操作未完成（404），请重试')), true)
  assert.equal(expiredReadingVisit(new Error('无法连接本地服务')), false)
})
