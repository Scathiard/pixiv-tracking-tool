import type { Anchor } from './types'
interface Positions {reader?: Anchor; feed?: Anchor; gallery?: number; updated: number}
const key = 'pixiv-local-positions-v1'
function load(): Record<string, Positions> {
  try { return JSON.parse(localStorage.getItem(key) || '{}') } catch { return {} }
}
export function positionKey(query: string) {
  const params = new URLSearchParams(query)
  for (const name of ['offset', 'page', 'ceiling', 'as_of', 'mode']) params.delete(name)
  params.sort()
  return params.toString() || 'all'
}
export function positions(query: string): Positions { return load()[positionKey(query)] || {updated: 0} }
export function savePosition(query: string, kind: 'reader' | 'feed' | 'gallery', anchor: Anchor | number) {
  const all = load(), scope = positionKey(query)
  all[scope] = {...all[scope], [kind]: anchor, updated: Date.now()}
  const kept = Object.entries(all).sort((a, b) => b[1].updated - a[1].updated).slice(0, 20)
  try { localStorage.setItem(key, JSON.stringify(Object.fromEntries(kept))) } catch { /* Browsing also works when storage is unavailable. */ }
}
