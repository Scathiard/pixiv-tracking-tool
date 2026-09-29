import { test } from 'node:test'
import assert from 'node:assert/strict'
import { cardSourceLabel, cardSourceReason, needsCardRefresh, needsPageRefresh, sourceNotice, type PageData, type WorkCard } from '../src/types.ts'

const attention = {status: 'attention' as const, kind: 'repair', next_retry_at: null, reason: '主站仅显示尚无此页'}
const waiting = {status: 'waiting' as const, kind: 'repair', next_retry_at: '2026-09-29T12:00:00Z', reason: '连接暂时中断'}

test('an unavailable source does not hide a saved image or create a deletion claim', () => {
  const page = {src: '/images/saved.jpg', state: 'verified', recovery: attention} as PageData
  assert.equal(needsPageRefresh(page), false)
  const note = sourceNotice({state: 'unavailable', checked_at: '2026-09-29T10:00:00Z', reason: '主站显示尚无此页'}, attention)
  assert.match(note!, /已存内容仍可阅览/)
  assert.doesNotMatch(note!, /已删除/)
  assert.equal(sourceNotice(null, {...attention, reason: '主站显示尚无此页；已有存档保留'}), '主站显示尚无此页；已有存档保留')
})

test('stopped remote repair does not poll a missing page, while queued and waiting repairs do', () => {
  const missing = {src: null, state: 'missing', recovery: attention} as PageData
  assert.equal(needsPageRefresh(missing), false)
  assert.equal(needsPageRefresh({...missing, recovery: waiting}), true)
  assert.equal(needsPageRefresh({...missing, recovery: {...waiting, status: 'queued'}}), true)
})

test('stopped remote repair still lets a saved original receive a generated thumbnail', () => {
  const card = {thumbnail: null, preview: '/images/saved.jpg', preview_status: 'ready', recovery: attention} as WorkCard
  assert.equal(needsCardRefresh(card), true)
  assert.equal(needsCardRefresh({...card, preview: null, preview_status: 'failed'}), false)
  assert.equal(needsCardRefresh({...card, thumbnail: '/images/thumb.webp'}), false)
  assert.equal(needsCardRefresh({...card, recovery: waiting}), true)
})

test('gallery source badges name only confirmed exceptional states and keep the reason available', () => {
  const saved = {thumbnail: '/images/thumb.webp', recovery: attention,
    availability: {state: 'unavailable', checked_at: '2026-09-29T10:00:00Z', reason: '尚无此页'}} as WorkCard
  assert.equal(cardSourceLabel(saved), '补齐待核对')
  assert.equal(cardSourceReason(saved), attention.reason)
  assert.equal(cardSourceLabel({...saved, recovery: null}), '主站不可获取')
  assert.equal(cardSourceLabel({...saved, recovery: null, availability: {...saved.availability!, state: 'private'}}), '主站非公开')
  assert.equal(cardSourceLabel({...saved, recovery: null, availability: null}), null)
})
