export type TagOrder = 'count' | 'name'
const storageKey = 'pixiv-local-tag-order'
const normalize = (value: unknown): TagOrder => value === 'name' ? 'name' : 'count'
const names = new Intl.Collator('zh-CN')

/** Reorder a copy of the cached names; counts and selection keep their tag identity. */
export function orderedTags(tags: readonly string[], counts: Record<string, number> | undefined, order: TagOrder): string[] {
  return [...tags].sort((left, right) => (order === 'count' ? (counts?.[right] || 0) - (counts?.[left] || 0) : 0)
    || names.compare(left, right) || (left < right ? -1 : left > right ? 1 : 0))
}

/** Browser-only preference, independent of catalogue request ownership. */
export class TagOrderPreference {
  private value: TagOrder
  private listeners = new Set<(value: TagOrder) => void>()
  constructor() {
    try { this.value = normalize(localStorage.getItem(storageKey)) }
    catch { this.value = 'count' }
  }
  get(): TagOrder { return this.value }
  set(value: unknown): boolean {
    this.value = normalize(value)
    let saved = true
    try { localStorage.setItem(storageKey, this.value) }
    catch { saved = false }
    for (const listener of this.listeners) listener(this.value)
    return saved
  }
  subscribe(listener: (value: TagOrder) => void): () => void {
    this.listeners.add(listener); listener(this.value)
    return () => { this.listeners.delete(listener) }
  }
}
export const tagOrderPreference = new TagOrderPreference()
