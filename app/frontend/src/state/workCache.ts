import { api } from '../api'
import type { WorkDetail } from '../types'
import { bookmarkState } from './bookmarks'

// Cache only the small opening/detail response; long image-page windows belong
// to PageReader and are released as the reader moves. A single aborted caller
// does not cancel another caller's request for the same version.
const cache = new Map<string, {value: WorkDetail; time: number}>()
const flights = new Map<string, Promise<WorkDetail>>()
let generation = 0
export function invalidateWorkCache(id?: number) {
  generation++
  for (const key of cache.keys()) if (id === undefined || key.startsWith(`${id}:`)) cache.delete(key)
  // In-flight promises may still finish for their original consumers, but may
  // neither repopulate the cache nor be reused after invalidation.
  flights.clear()
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
  let promise: Promise<WorkDetail>
  if (!fresh && old && Date.now() - old.time < 45000) {
    cache.delete(key); cache.set(key, old)
    promise = Promise.resolve(old.value)
  } else {
    promise = flights.get(key)!
    if (!promise || fresh) {
      const ticket = generation
      promise = api<WorkDetail>(workPath(id, revisionId)).then(value => {
        if (ticket === generation) {
          cache.set(key, {value, time: Date.now()})
          while (cache.size > 32) cache.delete(cache.keys().next().value!)
        }
        return value
      }).finally(() => { if (flights.get(key) === promise) flights.delete(key) })
      flights.set(key, promise)
    }
  }
  const value = await new Promise<WorkDetail>((resolve, reject) => {
    const abort = () => reject(signal.reason)
    signal.addEventListener('abort', abort, {once: true})
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
  })
  signal.throwIfAborted()
  return {...value, bookmark: bookmarkState(id) || value.bookmark}
}
