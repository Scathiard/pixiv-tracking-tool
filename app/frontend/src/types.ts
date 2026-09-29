export type Mode = 'gallery' | 'reader' | 'feed'
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
export interface WorkSummary {
  id: number; pixiv_id: string; title: string; artist_id: number; artist_name: string
  type: string; page_count: number; x_restrict: number | null; thumbnail: string | null; preview?: string | null
  caption: string; caption_state: string; tags: string[]; is_hidden: boolean
  is_bookmarked: boolean; bookmark?: BookmarkState; rating: number; posted_at: string | null
  bookmark_count: number | null; view_count: number | null
  recovery?: RecoveryState | null
  availability?: AvailabilityState | null
}
export interface PageData {
  index: number; work_id: number; work_index: number; page: number; page_count: number
  title: string; artist_name: string; artist_id: number; pixiv_id: string; type: string; x_restrict: number | null
  next_work_index: number; src: string | null; width: number | null; height: number | null; state: string
  bookmark?: BookmarkState
  recovery?: RecoveryState | null
  availability?: AvailabilityState | null
}
export interface WorkDetail extends WorkSummary {
  display_page_count: number
  pages: {index: number; src: string | null; width: number | null; height: number | null; state: string}[]
  assets: {id: string; role: string; page: number; state: string; error: string | null}[]
  legacy_variants: {page: number; files: {src: string | null; name: string; default: boolean}[]}[]
}
export interface PageBatch {items: PageData[]; total: number; offset: number; ceiling?: number}
export type WorkCard = Pick<WorkSummary, 'id' | 'pixiv_id' | 'title' | 'artist_id' | 'artist_name' | 'type' | 'page_count' | 'x_restrict' | 'thumbnail' | 'is_hidden' | 'posted_at' | 'bookmark'> & {
  preview?: string | null
  preview_status?: 'ready' | 'pending' | 'failed' | 'unavailable'
  recovery?: RecoveryState | null
  availability?: AvailabilityState | null
}
export function needsCardRefresh(work: WorkCard): boolean {
  if (work.recovery?.status === 'attention') return !work.thumbnail && work.preview_status === 'ready'
  return !work.thumbnail || (!!work.preview_status && work.preview_status !== 'ready')
}
export function cardSourceLabel(work: WorkCard): string | null {
  if (work.recovery?.status === 'attention') return '补齐待核对'
  if (work.availability?.state === 'private') return '主站非公开'
  if (work.availability?.state === 'unavailable') return '主站不可获取'
  return null
}
export function cardSourceReason(work: WorkCard): string | null {
  if (work.recovery?.status === 'attention') return work.recovery.reason || '缺项需要手动核对'
  return work.availability?.reason || null
}
export function needsPageRefresh(page: PageData): boolean {
  if (page.recovery?.status === 'attention') return false
  return !page.src || ['failed', 'missing', 'invalid', 'mismatch'].includes(page.state) ||
    page.recovery?.status === 'queued' || page.recovery?.status === 'waiting'
}
export function sourceNotice(availability?: AvailabilityState | null, recovery?: RecoveryState | null): string | null {
  const reason = recovery?.status === 'attention' ? recovery.reason || '主站暂时无法获取' :
    availability?.state === 'private' || availability?.state === 'unavailable' ?
      availability.reason || (availability.state === 'private' ? '主站当前无法公开获取' : '主站暂时无法获取') : null
  if (!reason) return null
  if (/已有存档保留|已存内容/.test(reason)) return reason
  return `${reason}；已存内容仍可阅览`
}
export interface GalleryBatch {items: WorkCard[]; total: number; offset: number; ceiling: number; as_of: string}
export interface Anchor {work_id: number; page: number; offset: number; index: number; work_index: number}
export interface Artist {id: number; pixiv_id: string; name: string; mode: 'active' | 'paused' | 'stopped'; biography?: string | null; avatar?: string | null; profile_refreshed_at?: string | null; work_count?: number; recent_works?: WorkCard[]}
export type PageLoader = (offset: number, limit: number, signal: AbortSignal) => Promise<PageBatch>
