import test from 'node:test'
import assert from 'node:assert/strict'
import { invertSelection, loadArtistCatalogue, readTagValues, selectionCount, selectionQuery, toggleSelection, writeTagValues } from '../src/state/filterSelection.ts'

test('all, empty, toggled and inverse selections remain distinct across catalogue', () => {
  const catalogue = ['1', '2', '3']
  assert.deepEqual(toggleSelection({all: true, values: []}, '3', catalogue), {all: false, values: ['3']})
  assert.deepEqual(invertSelection({all: true, values: []}, catalogue), {all: false, values: []})
  assert.deepEqual(invertSelection({all: false, values: []}, catalogue), {all: true, values: []})
  assert.deepEqual(invertSelection({all: false, values: ['2']}, catalogue), {all: false, values: ['2'], excluded: true})
  assert.deepEqual(invertSelection({all: false, values: ['2'], excluded: true}, catalogue), {all: false, values: ['2'], excluded: false})
})

test('ten thousand candidates invert and serialize only the smaller selection', () => {
  const catalogue = Array.from({length: 10000}, (_, id) => `中文标签${id}`)
  const inverse = invertSelection({all: false, values: [catalogue[9999]!] }, catalogue)
  assert.equal(selectionCount(inverse, catalogue.length), 9999)
  assert.deepEqual(selectionQuery(inverse, catalogue), {excluded: true, values: ['中文标签9999']})
  assert.deepEqual(selectionQuery({all: true, values: []}, catalogue), null)
  assert.deepEqual(selectionQuery({all: false, values: []}, catalogue), {excluded: false, values: []})
  assert.deepEqual(selectionQuery({all: false, values: catalogue.slice(1)}, catalogue), {excluded: true, values: ['中文标签0']})
})

test('explicit full includes and absent old selections keep their existing meaning', () => {
  assert.deepEqual(selectionQuery({all: false, values: ['A', 'B']}, ['A', 'B']), {excluded: false, values: ['A', 'B']})
  assert.deepEqual(selectionQuery({all: false, values: ['A', 'B', 'old']}, ['A', 'B', 'C']), {excluded: false, values: ['A', 'B', 'old']})
  assert.equal(selectionCount({all: false, values: ['old'], excluded: true}, ['A', 'B', 'C']), 3)
})

test('literal comma tags round trip while single-parameter legacy CSV stays compatible', () => {
  const query = new URLSearchParams('tags=old&sort=newest')
  writeTagValues(query, ['逗号,标签'])
  assert.deepEqual(query.getAll('tags'), ['逗号,标签', '逗号,标签'])
  assert.deepEqual(readTagValues(query), ['逗号,标签'])
  writeTagValues(query, ['逗号,标签', '普通'], 'tags_exclude')
  assert.deepEqual(readTagValues(query, 'tags_exclude'), ['逗号,标签', '普通'])
  assert.deepEqual(readTagValues(new URLSearchParams('tags=A,B')), ['A', 'B'])
  writeTagValues(query, [])
  assert.equal(query.get('tags'), '')
  assert.equal(query.get('sort'), 'newest')
})

test('all pages load with no 100 artist truncation, and incomplete lists fail', async () => {
  const catalogue = Array.from({length: 237}, (_, id) => ({id: id + 1}))
  const offsets: number[] = []
  assert.deepEqual(await loadArtistCatalogue(async (offset, limit) => {
    offsets.push(offset)
    return {items: catalogue.slice(offset, offset + limit), total: catalogue.length}
  }), catalogue)
  assert.deepEqual(offsets, [0, 100, 200])
  await assert.rejects(loadArtistCatalogue(async () => ({items: [], total: 1})), /不完整/)
})
