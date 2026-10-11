// Entry navigation is determined by explicit URLs only. Legacy last-visit storage is ignored.
export function galleryEntryIndex(query: string, pageSize: number, total: number) {
  const page = Number(new URLSearchParams(query).get('page'))
  const wanted = Number.isSafeInteger(page) && page > 0 ? (page - 1) * pageSize : 0
  return Math.min(Math.max(0, total - 1), wanted)
}
export function freshLibraryQuery(query: string, resetPage = false) {
  const params = new URLSearchParams(query)
  for (const name of ['ceiling', 'as_of', 'offset', 'limit', 'page_offset', 'mode', 'origin', 'origin_artist', 'cursor', 'direction', 'anchor_work_id', 'index', 'work_index']) params.delete(name)
  if (resetPage) params.delete('page')
  return params.toString()
}
export function positionKey(query: string) {
  const params = new URLSearchParams(freshLibraryQuery(query, true))
  params.sort()
  return params.toString() || 'all'
}
