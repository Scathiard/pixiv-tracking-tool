import { test } from 'node:test'
import assert from 'node:assert/strict'
import { freshLibraryQuery, galleryEntryIndex } from '../src/positions.ts'

test('obsolete last-visit storage cannot select any gallery, feed or detail entry', () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  let reads = 0, writes = 0
  const obsolete = JSON.stringify({all: {gallery: {work_id: 987, index: 900}, feed: {work_id: 987, page: 30}, reader: {work_id: 987, page: 99}}})
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {
    getItem: () => { reads++; return obsolete }, setItem: () => { writes++ }, removeItem: () => { writes++ },
  }})
  try {
    assert.equal(galleryEntryIndex('', 100, 1000), 0)
    assert.equal(galleryEntryIndex(freshLibraryQuery('page=10&origin=feed', true), 100, 1000), 0)
    assert.equal(reads, 0); assert.equal(writes, 0)
  } finally {
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else Reflect.deleteProperty(globalThis, 'localStorage')
  }
})

test('explicit page URLs remain navigable and invalid pages start at the beginning', () => {
  assert.equal(galleryEntryIndex('page=3', 100, 1000), 200)
  assert.equal(galleryEntryIndex('page=50', 100, 451), 450)
  assert.equal(galleryEntryIndex('page=3', 100, 0), 0)
  for (const page of ['0', '-1', '1.5', 'NaN', 'Infinity', '9007199254740992', '']) {
    assert.equal(galleryEntryIndex(`page=${page}`, 100, 1000), 0)
  }
})

test('home and return navigation starts at page one while keeping filters and sort', () => {
  const query = 'page=8&ceiling=100&as_of=old&artist_ids=2&tags=blue&tags=sky&sort=oldest&favorites=1&origin=feed'
  const fresh = new URLSearchParams(freshLibraryQuery(query, true))
  assert.equal(fresh.has('page'), false)
  assert.equal(fresh.has('ceiling'), false); assert.equal(fresh.has('as_of'), false)
  assert.equal(fresh.has('origin'), false)
  assert.equal(fresh.get('artist_ids'), '2')
  assert.deepEqual(fresh.getAll('tags'), ['blue', 'sky'])
  assert.equal(fresh.get('sort'), 'oldest'); assert.equal(fresh.get('favorites'), '1')
  assert.equal(galleryEntryIndex(fresh.toString(), 100, 1000), 0)
})

test('explicit forward and backward URL entries use current catalogue without changing their selected page', () => {
  for (const page of [5, 2, 5]) {
    const fresh = freshLibraryQuery(`page=${page}&ceiling=20&as_of=old&q=title&offset=888&mode=feed`)
    assert.equal(galleryEntryIndex(fresh, 100, 1000), (page - 1) * 100)
    const params = new URLSearchParams(fresh)
    assert.equal(params.get('q'), 'title')
    assert.equal(params.has('ceiling'), false); assert.equal(params.has('offset'), false)
  }
})
