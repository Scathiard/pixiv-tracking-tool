import { api, url } from '../api.ts'
import { freshLibraryQuery, galleryEntryIndex } from '../positions.ts'
import type { GalleryBatch } from '../types.ts'

export interface GallerySeed {query: string; anchor: number; pageSize: number; continuous: boolean; data: GalleryBatch}

/** The card response owns the snapshot; optional rail counts never gate entry. */
export async function readLibraryEntry(query: string, path: string, pageSize: number, continuous: boolean, signal: AbortSignal): Promise<GallerySeed> {
  const params = new URLSearchParams(freshLibraryQuery(query))
  params.delete('page'); params.delete('revision_id')
  const wanted = path === '/' ? galleryEntryIndex(query, pageSize, Number.MAX_SAFE_INTEGER) : 0
  const limit = path !== '/' ? 1 : continuous ? 240 : pageSize
  let data = await api<GalleryBatch>(url('/api/library', params.toString(), {offset: wanted, limit, compact: 1}), {signal})
  signal.throwIfAborted()
  params.set('ceiling', String(data.ceiling)); params.set('as_of', data.as_of)
  const entry = path === '/' ? galleryEntryIndex(query, pageSize, data.total) : 0
  const anchor = continuous ? entry : Math.floor(entry / pageSize) * pageSize
  // An explicit page can have disappeared since the URL was saved. Re-read only
  // that boundary case using the first response's snapshot.
  if (path === '/' && wanted !== anchor) {
    data = await api<GalleryBatch>(url('/api/library', params.toString(), {offset: anchor, limit, compact: 1}), {signal})
    signal.throwIfAborted()
  }
  return {query: params.toString(), anchor, pageSize, continuous, data}
}

export function matchingGallerySeed(seed: GallerySeed | null | undefined, query: string, anchor: number, pageSize: number, continuous: boolean) {
  return seed && seed.query === query && seed.anchor === anchor && seed.pageSize === pageSize && seed.continuous === continuous ? seed.data : undefined
}
