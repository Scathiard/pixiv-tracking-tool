import {test} from 'node:test'
import assert from 'node:assert/strict'
import {acceptsBookmarkUpdate, nextBookmarkIntent, showsBookmarkFact} from '../src/state/bookmarkRules.ts'
import {requestId} from '../src/state/requestId.ts'

test('request IDs use secure random bytes when randomUUID is unavailable', () => {
  let draws = 0
  const source = {
    getRandomValues(bytes: Uint8Array) {
      assert.equal(bytes.length, 16)
      bytes.fill(draws++)
      return bytes
    },
  }
  const first = requestId(source)
  const second = requestId(source)
  assert.equal(first, '00000000-0000-4000-8000-000000000000')
  assert.equal(second, '01010101-0101-4101-8101-010101010101')
  assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  assert.notEqual(first, second)
  assert.equal(draws, 2)
})

test('the final readback may finish the pending operation at the same version', () => {
  const pending = {version: 8, status: 'pending', operation_id: 'operation-a'}
  assert.equal(acceptsBookmarkUpdate(pending, {version: 8, status: 'confirmed', operation_id: 'operation-a'}), true)
  assert.equal(acceptsBookmarkUpdate(pending, {version: 8, status: 'unconfirmed', operation_id: 'operation-a'}), true)
})
test('late responses cannot roll a terminal state back or replace another operation', () => {
  const done = {version: 8, status: 'confirmed', operation_id: 'operation-a'}
  assert.equal(acceptsBookmarkUpdate(done, {version: 8, status: 'pending', operation_id: 'operation-a'}), false)
  assert.equal(acceptsBookmarkUpdate(done, {version: 8, status: 'failed', operation_id: 'operation-b'}), false)
  assert.equal(acceptsBookmarkUpdate(done, {version: 7, status: 'confirmed', operation_id: 'old'}), false)
  assert.equal(acceptsBookmarkUpdate(done, {version: 9, status: 'pending', operation_id: 'operation-c'}), true)
})

test('account changes and server restarts outrank older work versions', () => {
  const first = {version: 8, status: 'confirmed', operation_id: null,
    account_epoch: 1_000_000, current_account_id: 'account-a'}
  const switched = {version: 1, status: 'idle', operation_id: null,
    account_epoch: 1_000_001, current_account_id: 'account-b'}
  const restarted = {version: 1, status: 'confirmed', operation_id: null,
    account_epoch: 1_100_000, current_account_id: 'account-b'}
  assert.equal(acceptsBookmarkUpdate(first, switched), true)
  assert.equal(acceptsBookmarkUpdate(switched, first), false)
  assert.equal(acceptsBookmarkUpdate(first, restarted), true)
  assert.equal(acceptsBookmarkUpdate(restarted, switched), false)
  assert.equal(acceptsBookmarkUpdate(first, {...first, current_account_id: 'account-b'}), false)
})

test('a stored same-account fact stays visible while stale or pending; another account cannot infer cancel', () => {
  const fact = {value: true, verified: true, fact_account_id: 'account-a', current_account_id: 'account-a'}
  assert.equal(showsBookmarkFact(fact), true)
  assert.equal(nextBookmarkIntent(fact), false)
  assert.equal(showsBookmarkFact({...fact, current_account_id: null}), true)
  assert.equal(showsBookmarkFact({...fact, current_account_id: 'account-b'}), false)
  assert.equal(nextBookmarkIntent({...fact, current_account_id: 'account-b'}), true)
  assert.equal(showsBookmarkFact({...fact, verified: false}), false)
  assert.equal(nextBookmarkIntent({...fact, verified: false}), true)
})
