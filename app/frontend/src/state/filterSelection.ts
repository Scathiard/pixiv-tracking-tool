export interface FilterSelection { all: boolean; values: string[]; excluded?: boolean }

/** A profile belongs only to one explicitly included local artist. */
export function singleArtistScope(query: string): number | null {
  const params = new URLSearchParams(query)
  if (params.has('artist_ids_exclude')) return null
  const values = [...new Set(params.getAll('artist_ids').flatMap(value => value.split(',')).filter(Boolean))]
  if (values.length !== 1 || !/^[1-9]\d*$/.test(values[0]!)) return null
  const id = Number(values[0])
  return Number.isSafeInteger(id) ? id : null
}

export function readTagValues(query: URLSearchParams, key = 'tags'): string[] {
  const values = query.getAll(key)
  return [...new Set((values.length > 1 ? values : (values[0] || '').split(',')).filter(Boolean))]
}

export function writeTagValues(query: URLSearchParams, values: string[], key = 'tags'): void {
  query.delete(key)
  if (!values.some(value => value.includes(','))) { query.set(key, values.join(',')); return }
  for (const value of values) query.append(key, value)
  // One repeated key is still legacy CSV; duplicate the literal value to make
  // the raw-tag form explicit without changing old single-parameter URLs.
  if (values.length === 1) query.append(key, values[0]!)
}

export function selectionCount(selection: FilterSelection, catalogue: number | string[]): number {
  const total = typeof catalogue === 'number' ? catalogue : catalogue.length
  if (selection.all) return total
  if (!selection.excluded) return selection.values.length
  const values = new Set(selection.values)
  const excluded = typeof catalogue === 'number' ? values.size : catalogue.filter(value => values.has(value)).length
  return Math.max(0, total - excluded)
}

export function selectionQuery(selection: FilterSelection, catalogue: string[]): {excluded: boolean; values: string[]} | null {
  if (selection.all) return null
  const values = new Set(selection.values)
  const complement = catalogue.filter(value => !values.has(value))
  // Preserve empty include (no results) and avoid enumerating a large complement.
  const known = new Set(catalogue)
  return complement.length > 0 && complement.length < selection.values.length && selection.values.every(value => known.has(value))
    ? {excluded: !selection.excluded, values: complement}
    : {excluded: !!selection.excluded, values: selection.values}
}

export function toggleSelection(selection: FilterSelection, value: string, catalogue: string[]): FilterSelection {
  if (selection.all) return {all: false, values: [value]}
  const values = selection.values.includes(value) ? selection.values.filter(item => item !== value) : [...selection.values, value]
  const chosen = new Set(values), coversCatalogue = catalogue.length > 0 && catalogue.every(item => chosen.has(item))
  if ((selection.excluded && !values.length) || (!selection.excluded && coversCatalogue)) return {all: true, values: []}
  if (selection.excluded && coversCatalogue) return {all: false, values: []}
  return {all: false, values, excluded: selection.excluded}
}

export function invertSelection(selection: FilterSelection, catalogue: string[]): FilterSelection {
  if (selection.all) return {all: false, values: []}
  if (!selectionCount(selection, catalogue)) return {all: true, values: []}
  return {all: false, values: selection.values, excluded: !selection.excluded}
}

export async function loadArtistCatalogue<T extends {id: number}>(
  fetchPage: (offset: number, limit: number) => Promise<{items: T[]; total: number}>,
): Promise<T[]> {
  const items = new Map<number, T>()
  for (let offset = 0; ; ) {
    const page = await fetchPage(offset, 100)
    for (const item of page.items) items.set(item.id, item)
    offset += page.items.length
    if (offset >= page.total) return [...items.values()]
    if (!page.items.length) throw new Error('画师名单载入不完整，请重试')
  }
}
