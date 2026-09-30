/** Compare presented data with progress even when the first poll arrives last. */
export class CatalogueUpdates {
  private progress: number | null = null
  private presented: number | null = null
  private requested: number | null = null

  private request(): boolean {
    if (this.progress === null || this.requested !== null && this.requested >= this.progress) return false
    this.requested = this.progress
    return true
  }

  observeProgress(revision?: number): boolean {
    if (!Number.isSafeInteger(revision) || revision! < 0) return false
    const previous = this.progress
    this.progress = Math.max(previous ?? 0, revision!)
    return (previous !== null && this.progress > previous || this.presented !== null && this.progress > this.presented)
      && this.request()
  }

  observePresented(revision?: number): boolean {
    if (!Number.isSafeInteger(revision) || revision! < 0) return false
    this.presented = Math.max(this.presented ?? 0, revision!)
    return this.progress !== null && revision! < this.progress && this.request()
  }
}

export const catalogueUpdates = new CatalogueUpdates()
let scheduled = false
export function queueCatalogueUpdate() {
  if (scheduled) return
  scheduled = true
  queueMicrotask(() => {
    scheduled = false
    window.dispatchEvent(new Event('archive-updated'))
  })
}
export function observeCatalogueData(revision?: number) {
  if (catalogueUpdates.observePresented(revision)) queueCatalogueUpdate()
}
