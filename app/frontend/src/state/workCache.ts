import { api } from '../api'
import { isOlderCatalogue, type WorkDetail } from '../types'
import { bookmarkState } from './bookmarks'

// Cache only the small opening/detail response; long image-page windows belong
// to PageReader and are released as the reader moves. A single aborted caller
// does not cancel another caller's request for the same version.
const cache = new Map<string, {value: WorkDetail; time: number}>()
let generation = 0
export function invalidateWorkCache(id?: number) {
  generation++
  for (const key of cache.keys()) if (id === undefined || key.startsWith(`${id}:`)) cache.delete(key)
  // In-flight promises may still finish for their original consumers, but may
  // neither repopulate the cache nor be reused after invalidation.
}
export function workPath(id: number, revisionId?: string | null, offset = 0, limit = 1) {
  const params = new URLSearchParams({page_offset: String(offset), page_limit: String(limit)})
  if (revisionId) params.set('revision_id', revisionId)
  return `/api/works/${id}?${params}`
}
export async function workDetail(id: number, revisionId: string | null | undefined, signal: AbortSignal, fresh = false): Promise<WorkDetail> {
  signal.throwIfAborted()
  const key = `${id}:${revisionId || 'default'}`
  const old = cache.get(key)
  let value: WorkDetail
  if (!fresh && old && Date.now() - old.time < 45000) {
    cache.delete(key); cache.set(key, old)
    value = old.value
  } else {
    const ticket = generation
    value = await api<WorkDetail>(workPath(id, revisionId), {signal, readScope: `work-cache:${ticket}:${fresh ? 'fresh' : 'opening'}`})
    if (ticket === generation && !isOlderCatalogue(value.catalogue_revision, cache.get(key)?.value.catalogue_revision)) {
      cache.set(key, {value, time: Date.now()})
      while (cache.size > 32) cache.delete(cache.keys().next().value!)
    }
  }
  signal.throwIfAborted()
  return {...value, bookmark: bookmarkState(id) || value.bookmark}
}
