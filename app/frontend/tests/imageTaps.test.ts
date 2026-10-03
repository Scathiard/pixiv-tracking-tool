import test from 'node:test'
import assert from 'node:assert/strict'
import { ImageTaps } from '../src/state/imageTaps.ts'

const wait = () => new Promise(resolve => setTimeout(resolve, 330))
test('one image tap opens zoom only after the double-tap window', async () => {
  const events: string[] = []
  const taps = new ImageTaps(() => events.push('zoom'), () => events.push('bookmark'))
  taps.down(1, 50, 50); taps.up(1, 50, 50)
  assert.deepEqual(events, [])
  await wait(); assert.deepEqual(events, ['zoom']); taps.cancel()
})
test('double tap bookmarks once without briefly opening zoom', async () => {
  const events: string[] = []
  const taps = new ImageTaps(() => events.push('zoom'), () => events.push('bookmark'))
  taps.down(1, 50, 50); taps.up(1, 50, 50)
  taps.down(2, 51, 50); taps.up(2, 51, 50)
  await wait(); assert.deepEqual(events, ['bookmark']); taps.cancel()
})
test('scrolling, multitouch and pointer cancellation cancel image operations', async () => {
  const events: string[] = []
  const taps = new ImageTaps(() => events.push('zoom'), () => events.push('bookmark'))
  taps.down(1, 50, 50); taps.move(1, 50, 90); taps.up(1, 50, 90)
  taps.down(1, 50, 50); taps.up(1, 50, 50)
  taps.down(2, 50, 50); taps.down(3, 70, 50); taps.up(2, 50, 50); taps.up(3, 70, 50)
  taps.down(4, 50, 50); taps.cancelPointer(4)
  await wait(); assert.deepEqual(events, [])
})
