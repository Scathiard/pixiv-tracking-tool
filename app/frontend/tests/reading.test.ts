import {test} from 'node:test'
import assert from 'node:assert/strict'
import {needsIdentityPrevious} from '../src/state/reading.ts'
import type {PageData} from '../src/types.ts'
const page = (changes: Partial<PageData>) => ({index: 2, page: 2, previous_work_index: 0, ...changes} as PageData)
test('identity-rebased previous navigation resolves a distinct neighbor instead of current index zero', () => {
  assert.equal(needsIdentityPrevious(page({})), true)
  assert.equal(needsIdentityPrevious(page({index: 0, page: 0})), true)
  assert.equal(needsIdentityPrevious(page({index: 202, page: 2})), false)
  assert.equal(needsIdentityPrevious(page({previous_work_index: null})), false)
})
