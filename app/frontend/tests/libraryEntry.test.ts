import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readLibraryEntry, matchingGallerySeed } from '../src/state/libraryEntry.ts'

const response = (offset: number, total = 600) => ({items: [], offset, total, ceiling: 600, as_of: '2026-10-10T00:00:00Z'})

test('entry obtains cards and snapshot from one library read, without counting first', async t => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async (path: string) => {
    const target = new URL(path, 'http://fixture')
    assert.equal(target.pathname, '/api/library')
    assert.equal(target.searchParams.get('offset'), '100')
    assert.equal(target.searchParams.get('limit'), '100')
    assert.equal(target.searchParams.has('ceiling'), false)
    return Response.json(response(100))
  })
  const seed = await readLibraryEntry('page=2&q=synthetic&as_of=old&ceiling=2', '/', 100, false, new AbortController().signal)
  assert.equal(fetchMock.mock.callCount(), 1)
  assert.equal(seed.anchor, 100)
  assert.equal(new URLSearchParams(seed.query).get('as_of'), '2026-10-10T00:00:00Z')
  assert.equal(new URLSearchParams(seed.query).get('q'), 'synthetic')
  assert.equal(matchingGallerySeed(seed, seed.query, 100, 100, false), seed.data)
  assert.equal(matchingGallerySeed(seed, seed.query, 100, 50, false), undefined)
  assert.equal(matchingGallerySeed(seed, seed.query, 100, 100, true), undefined)
  assert.equal(matchingGallerySeed(seed, 'q=other', 100, 100, false), undefined)
})

test('an oversized explicit page reuses the established snapshot for its final page', async t => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async (path: string) => {
    const params = new URL(path, 'http://fixture').searchParams
    if (params.get('offset') === '900') return Response.json(response(900, 115))
    assert.equal(params.get('offset'), '100')
    assert.equal(params.get('ceiling'), '600')
    assert.equal(params.get('as_of'), '2026-10-10T00:00:00Z')
    return Response.json(response(100, 115))
  })
  const seed = await readLibraryEntry('page=10', '/', 100, false, new AbortController().signal)
  assert.equal(seed.anchor, 100)
  assert.equal(fetchMock.mock.callCount(), 2)
})

test('continuous entry reads a finite window and direct readers read only one card', async t => {
  const limits: string[] = []
  t.mock.method(globalThis, 'fetch', async (path: string) => {
    const params = new URL(path, 'http://fixture').searchParams
    limits.push(params.get('limit')!)
    return Response.json(response(Number(params.get('offset'))))
  })
  for (const path of ['/', '/illust/42', '/flow']) await readLibraryEntry('', path, 500, true, new AbortController().signal)
  assert.deepEqual(limits, ['240', '1', '1'])
})

test('cancelled navigation never returns a late snapshot', async t => {
  const controller = new AbortController()
  t.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json(response(0)) })
  await assert.rejects(readLibraryEntry('', '/', 100, false, controller.signal), {name: 'AbortError'})
})
