import {test} from 'node:test'
import assert from 'node:assert/strict'
import {candidateRows} from '../src/state/candidateRows.ts'
test('ten thousand variable labels remain ordered and complete across width changes', () => {
  const candidates = Array.from({length:10005}, (_, index) => ({value:String(index), label:index % 3 ? `标签 ${index}` : `长标签文字 ${index}`}))
  for (const width of [220, 350, 700]) {
    const rows = candidateRows(candidates, width, text => text.length * 10)
    assert.deepEqual(rows.flat().map(item => item.value), candidates.map(item => item.value))
    assert.ok(rows.some(row => row.length > 1))
    for (const row of rows) assert.ok(row.length === 1 || row.reduce((sum, item) => sum + Math.min(width, item.label.length * 10 + 45), 0) + (row.length - 1) * 8 <= width)
  }
})
test('long single label has a reachable row and empty search has zero rows', () => {
  assert.equal(candidateRows([{value:'long',label:'x'.repeat(100)}], 200, text => text.length*10)[0]?.[0]?.value,'long')
  assert.deepEqual(candidateRows([], 200, text => text.length*10), [])
})
