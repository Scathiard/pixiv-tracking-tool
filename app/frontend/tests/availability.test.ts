import { test } from 'node:test'
import assert from 'node:assert/strict'
import { archiveNotice, archiveStatus, cardSourceLabel, cardSourceReason, isOlderCatalogue, mediaPlaceholder, needsCardRefresh, needsPageRefresh, sourceNotice, type PageData, type WorkCard } from '../src/types.ts'

const attention = {status: 'attention' as const, kind: 'repair', next_retry_at: null, reason: '主站仅显示尚无此页'}
const waiting = {status: 'waiting' as const, kind: 'repair', next_retry_at: '2026-09-29T12:00:00Z', reason: '连接暂时中断'}

test('older metadata responses cannot replace a newer catalogue snapshot', () => {
  assert.equal(isOlderCatalogue(10, 12), true)
  assert.equal(isOlderCatalogue(12, 12), false)
  assert.equal(isOlderCatalogue(13, 12), false)
  assert.equal(isOlderCatalogue(undefined, 12), false)
})

test('an unavailable source does not hide a saved image or create a deletion claim', () => {
  const page = {src: '/images/saved.jpg', state: 'verified', recovery: attention} as PageData
  assert.equal(needsPageRefresh(page), false)
  const note = sourceNotice({state: 'unavailable', checked_at: '2026-09-29T10:00:00Z', reason: '主站显示尚无此页'}, attention)
  assert.match(note!, /已存内容仍可阅览/)
  assert.doesNotMatch(note!, /已删除/)
  assert.equal(sourceNotice(null, {...attention, reason: '主站显示尚无此页；已有存档保留'}), '主站显示尚无此页；已有存档保留')
})

test('readable p0 does not turn a work with missing p1 into a complete archive', () => {
  const work = {thumbnail: '/images/p0.webp', preview: '/images/p0.jpg', preview_status: 'ready',
    archive: {state: 'partial', total: 2, verified: 1, missing: 1, unknown: 0},
    processing: {state: 'waiting', operation_id: 'repair', next_retry_at: '2026-09-30T12:00:00Z', reason: '连接暂时中断'}} as WorkCard
  assert.equal(cardSourceLabel(work), '存档缺 1 项')
  assert.equal(cardSourceReason(work), '连接暂时中断')
  assert.match(archiveNotice(work)!, /本地存档缺 1 项/)
  assert.equal(work.preview, '/images/p0.jpg')
  assert.equal(mediaPlaceholder(work).label, '等待自动重试')
})

test('complete originals and pending animation preview remain separate without healthy card badges', () => {
  const work = {thumbnail: null, preview: null, preview_status: 'pending',
    archive: {state: 'complete', total: 2, verified: 2, missing: 0, unknown: 0},
    processing: {state: 'queued', operation_id: 'preview', next_retry_at: null, reason: null}} as WorkCard
  assert.equal(cardSourceLabel(work), null)
  assert.equal(mediaPlaceholder(work, work.preview_status, true).label, '原件已存，动图预览待生成')
  assert.equal(needsCardRefresh(work), true)
  assert.match(archiveStatus(work.archive), /已核验完整/)
})

test('old unknown and source updates retain readable local pages without claiming verification', () => {
  const page = {src: '/images/old.jpg', state: 'unknown',
    archive: {state: 'unknown', total: 1, verified: 0, missing: 0, unknown: 1},
    source_update: {pending: true, reason: '主站图片已更新；已有存档保留'},
    availability: {state: 'unavailable', checked_at: '2026-09-30T10:00:00Z', reason: '主站暂时无法获取'}} as PageData
  assert.equal(needsPageRefresh(page), false)
  assert.equal(page.src, '/images/old.jpg')
  assert.match(archiveStatus(page.archive), /尚未全部核验/)
  assert.doesNotMatch(archiveStatus(page.archive), /已核验完整/)
  assert.match(archiveNotice(page)!, /已有存档保留/)
  assert.match(archiveNotice(page)!, /已存内容仍可阅览/)
  assert.equal(cardSourceLabel({...page, thumbnail: '/images/old.webp'} as unknown as WorkCard), '主站内容有变化')
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
