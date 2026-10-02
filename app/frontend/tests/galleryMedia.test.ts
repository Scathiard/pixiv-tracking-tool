import { test } from 'node:test'
import assert from 'node:assert/strict'
import { galleryMediaSource, mediaRetryDelay, newerMediaRevision, staticMediaRetryDelay } from '../src/state/galleryMedia.ts'

const animation = {thumbnail: '/thumb.webp', preview: '/preview.gif', animation: true}

test('visible animation uses the GIF even when a healthy thumbnail exists', () => {
  assert.deepEqual(galleryMediaSource(animation, true, false, false), {url: '/preview.gif', kind: 'animation'})
  assert.deepEqual(galleryMediaSource({...animation, animation: false}, true, false, false), {url: '/thumb.webp', kind: 'thumbnail'})
})

test('inactive cards release animation media and can still display a static thumbnail', () => {
  assert.deepEqual(galleryMediaSource(animation, false, false, false), {url: '/thumb.webp', kind: 'thumbnail'})
  assert.equal(galleryMediaSource({...animation, thumbnail: null}, false, false, false), null)
  assert.equal(galleryMediaSource({...animation, thumbnail: null, animation: false}, false, false, false), null)
})

test('animation failure falls back without suppressing the card or retrying the GIF', () => {
  assert.deepEqual(galleryMediaSource(animation, true, false, true), {url: '/thumb.webp', kind: 'thumbnail'})
  assert.equal(galleryMediaSource(animation, true, true, true), null)
  assert.deepEqual(galleryMediaSource({...animation, preview: null}, true, false, false), {url: '/thumb.webp', kind: 'thumbnail'})
  assert.deepEqual(galleryMediaSource({...animation, thumbnail: null, animation: false}, true, false, false), {url: '/preview.gif', kind: 'original'})
})

test('only an advancing known catalogue revision allows a same-URL failed preview retry', () => {
  assert.equal(newerMediaRevision(11, 10), true)
  assert.equal(newerMediaRevision(10, 10), false)
  assert.equal(newerMediaRevision(9, 10), false)
  assert.equal(newerMediaRevision(undefined, 10), false)
  assert.equal(newerMediaRevision(10, undefined), false)
  assert.equal(newerMediaRevision(NaN, 10), false)
})

test('unrelated rapid catalogue updates cannot retry failed media more than once per cooldown', () => {
  assert.equal(mediaRetryDelay(1000, 1000), 12000)
  assert.equal(mediaRetryDelay(1000, 5000), 8000)
  assert.equal(mediaRetryDelay(1000, 13000), 0)
  assert.equal(mediaRetryDelay(1000, 20000), 0)
})

test('static media can recover twice without a catalogue update and then stops', () => {
  assert.equal(staticMediaRetryDelay(1, 1000, 1000), 700)
  assert.equal(staticMediaRetryDelay(1, 1000, 1500), 200)
  assert.equal(staticMediaRetryDelay(1, 1000, 1700), 0)
  assert.equal(staticMediaRetryDelay(2, 2000, 2000), 2500)
  assert.equal(staticMediaRetryDelay(2, 2000, 5000), 0)
  assert.equal(staticMediaRetryDelay(3, 6000, 6000), null)
  assert.equal(staticMediaRetryDelay(20, 6000, 999999), null)
  assert.equal(staticMediaRetryDelay(0, 1000, 1000), null)
})
