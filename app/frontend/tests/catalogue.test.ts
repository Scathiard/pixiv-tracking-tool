import { test } from 'node:test'
import assert from 'node:assert/strict'
import { CatalogueUpdates } from '../src/state/catalogue.ts'
import { hasNewWorks, type GalleryBatch } from '../src/types.ts'

test('old displayed data followed by a newer first progress snapshot triggers one refresh', () => {
  const versions = new CatalogueUpdates()
  assert.equal(versions.observePresented(0), false)
  assert.equal(versions.observeProgress(1), true)
  assert.equal(versions.observeProgress(1), false)
  assert.equal(versions.observePresented(0), false)
  assert.equal(versions.observePresented(1), false)
})

test('a late first library response is refreshed when progress already saw newer content', () => {
  const versions = new CatalogueUpdates()
  assert.equal(versions.observeProgress(1), false)
  assert.equal(versions.observePresented(0), true)
  assert.equal(versions.observePresented(0), false)
  assert.equal(versions.observeProgress(2), true)
  assert.equal(versions.observeProgress(1), false)
})

test('equal initial versions are only a baseline and never require a second initial read', () => {
  const versions = new CatalogueUpdates()
  assert.equal(versions.observePresented(12), false)
  assert.equal(versions.observeProgress(12), false)
  assert.equal(versions.observePresented(12), false)
})

test('media publication alone is not new work, while fresh matching works can replace an empty scope', () => {
  const batch = {items: [{id: 4}], total: 12, ceiling: 12} as GalleryBatch
  assert.equal(hasNewWorks(batch, 12, 12), false)
  assert.equal(hasNewWorks({...batch, ceiling: 13}, 12, 12), false)
  assert.equal(hasNewWorks({...batch, ceiling: 13, total: 13}, 12, 12), true)
  assert.equal(hasNewWorks({...batch, items: [{id: 13}]} as GalleryBatch, 12, 12), false)
  assert.equal(hasNewWorks({...batch, ceiling: 13, items: [{id: 13}]} as GalleryBatch, 12, 12), true)
  assert.equal(hasNewWorks({...batch, ceiling: 1, total: 1, items: [{id: 1}]} as GalleryBatch, 0, 0), true)
})
