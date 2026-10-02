export type Mode = 'gallery' | 'reader' | 'feed'
export interface ArchiveState {
  state: 'complete' | 'partial' | 'unknown'; total: number; verified: number; missing: number; unknown: number
}
export interface ProcessingState {
  state: 'idle' | 'queued' | 'running' | 'waiting' | 'attention'
  operation_id: string | null; next_retry_at: string | null; reason: string | null
}
export interface SourceUpdateState {pending: boolean; reason: string | null}
export interface ArchiveProjection {
  archive?: ArchiveState; processing?: ProcessingState; source_update?: SourceUpdateState
  recovery?: RecoveryState | null; availability?: AvailabilityState | null
}
export function isOlderCatalogue(incoming: number | undefined, current: number | undefined): boolean {
  return Number.isSafeInteger(incoming) && Number.isSafeInteger(current) && incoming! < current!
}
export interface RecoveryState {
  status: 'queued' | 'waiting' | 'ready' | 'attention'; kind: string
  next_retry_at: string | null; reason: string | null
}
export interface AvailabilityState {
  state: 'available' | 'unavailable' | 'private'; checked_at: string; reason: string | null
}
export interface BookmarkState {
  work_id: number; operation_id: string | null; status: 'idle' | 'pending' | 'confirmed' | 'failed' | 'unconfirmed'
  desired: boolean | null; value: boolean | null; confirmed_at: string | null; error: string | null
  version: number; cached: boolean; verified: boolean
  fact_account_id: string | null; current_account_id: string | null; account_epoch: number
}
export interface TagDetail {
  name: string; translated_name: string | null; language: 'zh-cn' | null; source: 'pixiv' | null
  source_field: string | null; requested_locale: string | null
  language_basis: 'requested_locale' | 'explicit_language_key' | null; checked_at: string | null
}
export interface WorkSummary extends ArchiveProjection {
  id: number; pixiv_id: string; title: string; artist_id: number; artist_name: string
  type: string; page_count: number; x_restrict: number | null; thumbnail: string | null; preview?: string | null
  caption: string; caption_state: string; tags: string[]; tag_details?: TagDetail[]; is_hidden: boolean
  is_bookmarked: boolean; bookmark?: BookmarkState; rating: number; posted_at: string | null
  bookmark_count: number | null; view_count: number | null
  recovery?: RecoveryState | null
  availability?: AvailabilityState | null
}
export interface PageData extends ArchiveProjection {
  index: number; work_id: number; work_index: number; page: number; page_count: number
  title: string; artist_name: string; artist_id: number; pixiv_id: string; type: string; x_restrict: number | null
  next_work_index: number; src: string | null; width: number | null; height: number | null; state: string
  bookmark?: BookmarkState
  recovery?: RecoveryState | null
  availability?: AvailabilityState | null
}
export interface WorkDetail extends WorkSummary {
  catalogue_revision?: number
  display_page_count: number
  pages: {index: number; src: string | null; width: number | null; height: number | null; state: string}[]
  assets: {id: string; role: string; page: number; state: string; error: string | null}[]
  legacy_variants: {page: number; files: {src: string | null; name: string; default: boolean}[]}[]
}
export interface PageBatch {items: PageData[]; total: number; offset: number; ceiling?: number; catalogue_revision?: number}
export type WorkCard = Pick<WorkSummary, 'id' | 'pixiv_id' | 'title' | 'artist_id' | 'artist_name' | 'type' | 'page_count' | 'x_restrict' | 'thumbnail' | 'is_hidden' | 'posted_at' | 'bookmark'> & ArchiveProjection & {
  preview?: string | null
  preview_status?: 'ready' | 'pending' | 'failed' | 'unavailable'
  recovery?: RecoveryState | null
  availability?: AvailabilityState | null
}
export function needsCardRefresh(work: WorkCard): boolean {
  if (work.processing?.state === 'attention' && work.preview_status !== 'ready') return false
  if (work.recovery?.status === 'attention') return !work.thumbnail && work.preview_status === 'ready'
  return !work.thumbnail || (!!work.preview_status && work.preview_status !== 'ready')
}
export function cardSourceLabel(work: WorkCard): string | null {
  if (work.archive?.state === 'partial') return work.archive.missing > 0 ? `存档缺 ${work.archive.missing} 项` : '部分存档'
  if (work.source_update?.pending) return '主站内容有变化'
  if (work.archive?.state === 'complete') return null
  if (work.recovery?.status === 'attention') return '补齐待核对'
  if (work.availability?.state === 'private') return '主站非公开'
  if (work.availability?.state === 'unavailable') return '主站不可获取'
  return null
}
export function cardSourceReason(work: WorkCard): string | null {
  if (work.archive?.state === 'partial') return work.processing?.reason || work.recovery?.reason || '本地尚未存齐；可在详情中补下载此作品'
  if (work.source_update?.pending) return work.source_update.reason || '主站内容有变化；已有存档保留'
  if (work.recovery?.status === 'attention') return work.recovery.reason || '缺项需要手动核对'
  return work.availability?.reason || null
}
export function needsPageRefresh(page: PageData): boolean {
  if (page.processing?.state === 'attention') return false
  if (page.recovery?.status === 'attention') return false
  return !page.src || ['failed', 'missing', 'invalid', 'mismatch'].includes(page.state) ||
    page.recovery?.status === 'queued' || page.recovery?.status === 'waiting'
}
export function archiveNotice(work: ArchiveProjection): string | null {
  const parts: string[] = []
  if (work.archive?.state === 'partial') parts.push(work.archive.missing > 0 ? `本地存档缺 ${work.archive.missing} 项` : '本地存档尚未齐全')
  const source = sourceNotice(work.availability, work.recovery)
  if (source) parts.push(source)
  if (work.source_update?.pending) parts.push(work.source_update.reason || '主站内容有变化；已有存档保留')
  return parts.length ? [...new Set(parts)].join('；') : null
}
export function archiveStatus(archive?: ArchiveState): string {
  if (!archive) return '按原件逐项查看核验情况'
  if (archive.state === 'complete') return `原件已核验完整（${archive.verified} / ${archive.total} 项）`
  if (archive.state === 'partial') return `原件尚未齐全：缺 ${archive.missing} 项，已核验 ${archive.verified} 项，待核验 ${archive.unknown} 项`
  return `原件尚未全部核验：已核验 ${archive.verified} 项，待核验 ${archive.unknown} 项`
}
export function mediaPlaceholder(work: ArchiveProjection, previewStatus?: string, animation = false): {label: string; reason: string | null} {
  const state = work.processing?.state || work.recovery?.status
  const reason = work.processing?.reason || work.recovery?.reason || null
  // A saved original and a missing derived preview are separate facts.
  if (animation && previewStatus !== 'ready' && work.archive?.state === 'complete') {
    return {label: previewStatus === 'failed' ? '原件已存，动图预览生成失败' : '原件已存，动图预览待生成', reason}
  }
  if (state === 'queued') return {label: '已排队补齐', reason}
  if (state === 'running') return {label: '正在补齐', reason}
  if (state === 'waiting') return {label: '等待自动重试', reason: reason || '暂时未能取得，可稍后重试'}
  if (state === 'attention') return {label: '本地缺少此页', reason: reason || '暂时无法补齐；可在作品整理中手动核对'}
  if (animation) return {label: previewStatus === 'failed' ? '动图预览生成失败' : previewStatus === 'unavailable' ? '动图预览不可用' : '动图预览尚未生成', reason}
  return {label: previewStatus === 'failed' ? '原图下载失败' : previewStatus === 'unavailable' ? '本地原图不可用' : previewStatus === 'pending' ? '原图尚未下载' : '本地缺少此页', reason}
}
export function sourceNotice(availability?: AvailabilityState | null, recovery?: RecoveryState | null): string | null {
  const reason = recovery?.status === 'attention' ? recovery.reason || '主站暂时无法获取' :
    availability?.state === 'private' || availability?.state === 'unavailable' ?
      availability.reason || (availability.state === 'private' ? '主站当前无法公开获取' : '主站暂时无法获取') : null
  if (!reason) return null
  if (/已有存档保留|已存内容/.test(reason)) return reason
  return `${reason}；已存内容仍可阅览`
}
export interface GalleryBatch {items: WorkCard[]; total: number; offset: number; ceiling: number; as_of: string; catalogue_revision?: number}
export function hasNewWorks(fresh: GalleryBatch, ceiling: number, total: number): boolean {
  return fresh.ceiling > ceiling && (fresh.total > total || !!fresh.items[0] && fresh.items[0].id > ceiling)
}
export interface Anchor {work_id: number; page: number; offset: number; index: number; work_index: number}
export interface Artist {id: number; pixiv_id: string; name: string; mode: 'active' | 'paused' | 'stopped'; biography?: string | null; avatar?: string | null; profile_refreshed_at?: string | null; work_count?: number; recent_works?: WorkCard[]}
export type PageLoader = (offset: number, limit: number, signal: AbortSignal) => Promise<PageBatch>
