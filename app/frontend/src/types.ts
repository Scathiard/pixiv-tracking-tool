export type Mode = 'gallery' | 'reader' | 'feed'
export interface WorkSummary {
  id: number; pixiv_id: string; title: string; artist_id: number; artist_name: string
  type: string; page_count: number; thumbnail: string | null; preview?: string | null
  caption: string; caption_state: string; tags: string[]; is_hidden: boolean
  is_bookmarked: boolean; rating: number; posted_at: string | null
  bookmark_count: number | null; view_count: number | null
}
export interface PageData {
  index: number; work_id: number; work_index: number; page: number; page_count: number
  title: string; artist_name: string; artist_id: number; pixiv_id: string; type: string
  next_work_index: number; src: string | null; width: number | null; height: number | null; state: string
}
export interface WorkDetail extends WorkSummary {
  display_page_count: number
  pages: {index: number; src: string | null; width: number | null; height: number | null; state: string}[]
  assets: {id: string; role: string; page: number; state: string; error: string | null}[]
  legacy_variants: {page: number; files: {src: string | null; name: string; default: boolean}[]}[]
}
export interface PageBatch {items: PageData[]; total: number; offset: number; ceiling?: number}
export type WorkCard = Pick<WorkSummary, 'id' | 'pixiv_id' | 'title' | 'artist_id' | 'artist_name' | 'type' | 'page_count' | 'thumbnail' | 'is_hidden' | 'posted_at'>
export interface GalleryBatch {items: WorkCard[]; total: number; offset: number; ceiling: number; as_of: string}
export interface Anchor {work_id: number; page: number; offset: number; index: number; work_index: number}
export interface Artist {id: number; name: string; mode: 'active' | 'paused' | 'stopped'}
export type PageLoader = (offset: number, limit: number, signal: AbortSignal) => Promise<PageBatch>
