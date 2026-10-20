import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {stripTypeScriptTypes} from 'node:module'
import vm from 'node:vm'
import {acceptsBookmarkUpdate, nextBookmarkIntent} from '../src/state/bookmarkRules.ts'
import {createBookmarkFeedback} from '../src/state/bookmarkFeedback.ts'

const initial = (id: number) => ({work_id: id, operation_id: null, status: 'confirmed', desired: null,
  value: false, confirmed_at: '2026-10-19T00:00:00Z', error: null, version: 1,
  cached: true, verified: true, fact_account_id: 'synthetic', current_account_id: 'synthetic', account_epoch: 10})
type Transport = (path: string, body?: any) => Promise<any>
function fixture(api: Transport, write: Transport) {
  let now = 100000, nextTimer = 0
  const timers = new Map<number, {at: number; callback: () => void}>()
  const window = {setTimeout(callback: () => void, delay: number) {
    const id = ++nextTimer; timers.set(id, {at: now + delay, callback}); return id
  }, clearTimeout(id: number) {timers.delete(id)}}
  const context = vm.createContext({api, write, window, clearTimeout: window.clearTimeout,
    document: {hidden: false}, AbortController, Error, performance: {now: () => now},
    Date: class extends Date {static now() {return now}}, reactive: (value: unknown) => value,
    acceptsBookmarkUpdate, nextBookmarkIntent, createBookmarkFeedback, requestId: () => 'fixed-synthetic-request'})
  const source = stripTypeScriptTypes(readFileSync(new URL('../src/state/bookmarks.ts', import.meta.url), 'utf8'))
    .replace(/^import .*$/gm, '').replace(/^export /gm, '')
  vm.runInContext(source + '\nglobalThis.subject = {ingestBookmark, syncBookmark, readBookmark, toggleBookmark, addBookmark, bookmarkState, bookmarkBusy, bookmarkWriting, bookmarkAcknowledging, resumeBookmarkPolling}', context)
  const drain = async () => {for (let i = 0; i < 25; i++) await Promise.resolve()}
  return {s: context.subject, drain, timers, async advance(delta: number) {
    const target = now + delta
    for (;;) {
      const due = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0]
      if (!due) break
      now = due[1].at; timers.delete(due[0]); due[1].callback(); await drain()
    }
    now = target; await drain()
  }}
}

test('background GET and refresh cannot disable, defer or retarget explicit clicks', async () => {
  for (const mode of ['read', 'refresh']) {
    const writes: any[] = []
    const f = fixture(async () => new Promise(() => {}), async (path, body) => {
      if (path.includes('/refresh')) return new Promise(() => {})
      writes.push(body)
      return {...initial(1), version: 2, status: 'pending', operation_id: 'accepted-write', kind: 'write', desired: true}
    })
    f.s.ingestBookmark(1, initial(1))
    if (mode === 'read') void f.s.readBookmark(1, true)
    else void f.s.syncBookmark(1, true)
    assert.equal(f.s.bookmarkWriting(1), false)
    const click = f.s.toggleBookmark(1)
    assert.equal(f.s.bookmarkAcknowledging(1), true)
    await click
    assert.equal(writes.length, 1)
    assert.equal(writes[0].desired, true)
    assert.equal(writes[0].expected_account_id, 'synthetic')
    assert.equal(writes[0].request_id, 'fixed-synthetic-request')
    await f.s.addBookmark(1)
    assert.equal(writes.length, 1)
  }
})

test('double tap during an accepted refresh sends one fixed add and keeps immediate acknowledgement', async () => {
  let release!: (value: any) => void
  const writes: any[] = []
  const f = fixture(async () => new Promise(() => {}), async (_path, body) => {
    writes.push(body)
    return new Promise(resolve => {release = resolve})
  })
  f.s.ingestBookmark(4, {...initial(4), status: 'pending', version: 2,
    kind: 'refresh', operation_id: 'background-refresh'})
  assert.equal(f.s.bookmarkWriting(4), false)
  const add = f.s.addBookmark(4)
  assert.equal(f.s.bookmarkAcknowledging(4), true)
  await f.s.addBookmark(4)
  assert.equal(writes.length, 1)
  assert.equal(writes[0].desired, true)
  // A newer background observation does not retarget the already captured add.
  f.s.ingestBookmark(4, {...initial(4), version: 2, value: true,
    kind: 'refresh', operation_id: 'background-refresh'})
  release({...initial(4), version: 3, value: true, status: 'confirmed', desired: true,
    kind: 'write', operation_id: 'accepted-add'})
  await add
  await f.advance(3000)
  assert.equal(f.s.bookmarkAcknowledging(4), false)
})

test('transport error is reported without awaiting recovery GET; next click only reads back', async () => {
  const paths: string[] = []
  const f = fixture(async () => new Promise(() => {}), async path => {
    paths.push(path)
    if (path.endsWith('/refresh')) return {...initial(2), version: 2, operation_id: 'readback', status: 'confirmed', kind: 'refresh'}
    throw new Error('synthetic write transport failure')
  })
  f.s.ingestBookmark(2, initial(2))
  let failure: unknown
  void f.s.toggleBookmark(2).catch((error: unknown) => {failure = error})
  await f.drain()
  assert.match(String(failure), /synthetic write transport failure/)
  assert.equal(f.s.bookmarkBusy(2), false)
  assert.equal(f.s.bookmarkState(2).status, 'unconfirmed')
  await f.advance(3000)
  assert.equal(f.s.bookmarkAcknowledging(2), false)
  f.s.ingestBookmark(2, initial(2)) // A late old GET must not erase uncertainty.
  assert.equal(f.s.bookmarkState(2).local_unknown, true)
  await f.s.toggleBookmark(2)
  assert.deepEqual(paths, ['/api/works/2/bookmark', '/api/works/2/bookmark/refresh'])
})

test('nine failed polls leave recoverable unknown and a usable readback action', async () => {
  let reads = 0
  const paths: string[] = []
  const f = fixture(async () => {reads++; throw new Error('synthetic read failure')}, async path => {
    paths.push(path)
    return {...initial(3), version: path.endsWith('/refresh') ? 3 : 2, status: 'pending',
      operation_id: path.endsWith('/refresh') ? 'readback' : 'write', kind: path.endsWith('/refresh') ? 'refresh' : 'write', desired: true}
  })
  f.s.ingestBookmark(3, initial(3))
  await f.s.toggleBookmark(3)
  await f.advance(180000)
  assert.equal(reads, 9)
  assert.equal(f.s.bookmarkBusy(3), false)
  assert.equal(f.s.bookmarkWriting(3), false)
  assert.equal(f.s.bookmarkAcknowledging(3), false)
  assert.equal(f.s.bookmarkState(3).status, 'unconfirmed')
  assert.equal(f.timers.size, 0)
  await f.s.toggleBookmark(3)
  assert.equal(paths.at(-1), '/api/works/3/bookmark/refresh')
})

test('account epoch change replaces local uncertainty and ignores the old write response', async () => {
  const f = fixture(async () => new Promise(() => {}), async () => {throw new Error('transport')})
  f.s.ingestBookmark(5, initial(5))
  await f.s.toggleBookmark(5).catch(() => undefined)
  assert.equal(f.s.bookmarkState(5).local_unknown, true)
  f.s.ingestBookmark(5, {...initial(5), account_epoch: 11, current_account_id: 'other', version: 0})
  assert.equal(f.s.bookmarkState(5).current_account_id, 'other')
  assert.equal(f.s.bookmarkState(5).local_unknown, false)
  assert.equal(f.s.bookmarkAcknowledging(5), false)
  f.s.ingestBookmark(5, {...initial(5), value: true, version: 9})
  assert.equal(f.s.bookmarkState(5).current_account_id, 'other')
})
