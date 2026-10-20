export interface Candidate {value: string; label: string; detail?: string; translated?: boolean}
/** Pack complete labels into continuous rows, keeping the original catalogue order. */
export function candidateRows(values: Candidate[], width: number, measure: (text: string) => number): Candidate[][] {
  const rows: Candidate[][] = []
  let row: Candidate[] = [], occupied = 0
  for (const item of values) {
    const size = Math.min(width, Math.ceil(measure(item.label) + (item.detail ? measure(item.detail) + 8 : 0) + 45))
    if (row.length && occupied + 8 + size > width) { rows.push(row); row = []; occupied = 0 }
    occupied += (row.length ? 8 : 0) + size; row.push(item)
  }
  if (row.length) rows.push(row)
  return rows
}
