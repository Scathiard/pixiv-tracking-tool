import { api, url } from '../api.ts'
import { loadArtistCatalogue } from './filterSelection.ts'
import type { TagDetail } from '../types.ts'

export interface FilterArtist {id: number; pixiv_id: string; name: string; mode: string}
export interface FilterTags {tags: string[]; tag_details?: TagDetail[]}
type Values = {artists: FilterArtist[]; tags: FilterTags}
export type CatalogueKind = keyof Values
export interface CatalogueEntry<T> {value: T | null; loading: boolean; error: string; stale: boolean}
export interface CatalogueSnapshot {artists: CatalogueEntry<FilterArtist[]>; tags: CatalogueEntry<FilterTags>}
type Loader = <K extends CatalogueKind>(kind: K, signal: AbortSignal) => Promise<Values[K]>
interface OwnedEntry<T> extends CatalogueEntry<T> {
  generation: number; updated: number; attempted: number
  flight?: Promise<void>; controller?: AbortController; timer?: ReturnType<typeof setTimeout>
}
const empty = <T>(): OwnedEntry<T> => ({value: null, loading: false, error: '', stale: true,
  generation: 0, updated: 0, attempted: -Infinity})

/** One browser-session snapshot; panel closure releases only its subscription. */
export class FilterCatalogue {
  private entries = {artists: empty<FilterArtist[]>(), tags: empty<FilterTags>()}
  private listeners = new Set<(snapshot: CatalogueSnapshot) => void>()
  private disposed = false
  private loader: Loader
  private now: () => number
  private minimumRefresh: number
  private refreshDelay: number
  private artistTtl: number
  constructor(loader: Loader, options: {now?: () => number; minimumRefresh?: number; refreshDelay?: number; artistTtl?: number} = {}) {
    this.loader = loader; this.now = options.now || Date.now
    this.minimumRefresh = options.minimumRefresh ?? 60000
    this.refreshDelay = options.refreshDelay ?? 300
    this.artistTtl = options.artistTtl ?? 30000
  }
  snapshot(): CatalogueSnapshot {
    const view = <T>(entry: CatalogueEntry<T>): CatalogueEntry<T> => ({value: entry.value,
      loading: entry.loading, error: entry.error, stale: entry.stale})
    return {artists: view(this.entries.artists), tags: view(this.entries.tags)}
  }
  subscribe(listener: (snapshot: CatalogueSnapshot) => void): () => void {
    this.listeners.add(listener); listener(this.snapshot())
    return () => { this.listeners.delete(listener) }
  }
  private publish() { const snapshot = this.snapshot(); for (const listener of this.listeners) listener(snapshot) }
  private schedule(kind: CatalogueKind) {
    const entry = this.entries[kind]
    if (this.disposed || entry.timer || entry.error || entry.flight) return
    const delay = Math.max(this.refreshDelay, this.minimumRefresh - (this.now() - entry.attempted))
    entry.timer = setTimeout(() => { entry.timer = undefined; void this.load(kind) }, delay)
  }
  load(kind: CatalogueKind): Promise<void> {
    const entry = this.entries[kind]
    if (this.disposed) return Promise.resolve()
    if (entry.flight) return entry.flight
    if (kind === 'artists' && entry.value !== null && this.now() - entry.updated >= this.artistTtl) entry.stale = true
    if (entry.error || entry.value !== null && !entry.stale) return Promise.resolve()
    clearTimeout(entry.timer); entry.timer = undefined
    const generation = entry.generation, controller = new AbortController()
    entry.controller = controller; entry.loading = true; entry.attempted = this.now()
    this.publish()
    const flight = Promise.resolve().then(() => this.loader(kind, controller.signal)).then(value => {
      if (!this.disposed && generation === entry.generation) {
        // The discriminated entry and loader have the same key.
        if (kind === 'artists') this.entries.artists.value = value as FilterArtist[]
        else this.entries.tags.value = value as FilterTags
        entry.updated = this.now(); entry.stale = false; entry.error = ''
      }
    }, () => {
      if (!this.disposed && generation === entry.generation) entry.error = kind === 'artists'
        ? '画师暂时无法读取，请重试' : '标签暂时无法读取，请重试'
    }).finally(() => {
      if (entry.flight !== flight) return
      entry.flight = undefined; entry.controller = undefined; entry.loading = false
      if (!this.disposed) {
        this.publish()
        // At most one follow-up after changes during a read. An ordinary error
        // waits for an explicit retry, never an automatic retry loop.
        if (generation !== entry.generation) this.schedule(kind)
      }
    })
    entry.flight = flight
    return flight
  }
  invalidate(kind: CatalogueKind | 'all' = 'all') {
    if (this.disposed) return
    for (const key of kind === 'all' ? ['artists', 'tags'] as const : [kind]) {
      const entry = this.entries[key]
      entry.generation++; entry.stale = true
      // Keep one in-flight read instead of repeatedly starting/cancelling SQL
      // under frequent catalogue events; its stale result will be discarded.
      this.schedule(key)
    }
    this.publish()
  }
  retry(kind: CatalogueKind): Promise<void> {
    const entry = this.entries[kind]
    entry.error = ''; entry.stale = true
    return this.load(kind)
  }
  warm(): Promise<void> { return Promise.all([this.load('artists'), this.load('tags')]).then(() => {}) }
  dispose() {
    this.disposed = true
    for (const entry of Object.values(this.entries)) { clearTimeout(entry.timer); entry.generation++; entry.controller?.abort() }
    this.listeners.clear()
  }
}

const loader: Loader = async <K extends CatalogueKind>(kind: K, signal: AbortSignal): Promise<Values[K]> => {
  if (kind === 'artists') return await loadArtistCatalogue((offset, limit) =>
    api<{items: FilterArtist[]; total: number}>(url('/api/artists', '', {offset, limit, compact: 1}), {signal})) as Values[K]
  return await api<FilterTags>('/api/library/tags?show_hidden=1', {signal}) as Values[K]
}
export const filterCatalogue = new FilterCatalogue(loader)
export function warmFilterCatalogue() { return filterCatalogue.warm() }
export function invalidateFilterCatalogue(kind: CatalogueKind | 'all' = 'all') { filterCatalogue.invalidate(kind) }
export function disposeFilterCatalogue() { filterCatalogue.dispose() }
