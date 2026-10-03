import { test } from 'node:test'
import assert from 'node:assert/strict'
import { visibleViewportBottom } from '../src/state/viewport.ts'

test('browser controls and keyboard bound the app without double counting viewport offset', () => {
  assert.equal(visibleViewportBottom(844, {height: 780, offsetTop: 0, scale: 1}), 780)
  assert.equal(visibleViewportBottom(844, {height: 430, offsetTop: 20, scale: 1}), 450)
  assert.equal(visibleViewportBottom(844, {height: 844, offsetTop: 0, scale: 1}), 844)
  assert.equal(visibleViewportBottom(390, {height: 390, offsetTop: 0, scale: 1}), 390)
})

test('native pinch zoom and transient zero-sized viewports leave the existing layout intact', () => {
  assert.equal(visibleViewportBottom(844, {height: 422, offsetTop: 0, scale: 2}), null)
  assert.equal(visibleViewportBottom(844, {height: 0, offsetTop: 0, scale: 1}), null)
  assert.equal(visibleViewportBottom(0, null), null)
  assert.equal(visibleViewportBottom(844, null), 844)
})
