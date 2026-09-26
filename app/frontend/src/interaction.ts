/** Lock one gesture's meaning before deciding whether it can navigate. */
export class DirectionGesture {
  private points = new Set<number>()
  private origin = {x: 0, y: 0}
  private last = {x: 0, y: 0}
  private lock: 'undecided' | 'horizontal' | 'vertical' | 'blocked' = 'blocked'
  down(id: number, x: number, y: number, blocked: boolean) {
    this.points.add(id)
    if (this.points.size > 1) { this.lock = 'blocked'; return }
    this.origin = this.last = {x, y}
    this.lock = blocked ? 'blocked' : 'undecided'
  }
  move(id: number, x: number, y: number) {
    if (!this.points.has(id) || this.points.size !== 1) return false
    this.last = {x, y}
    const dx = Math.abs(x - this.origin.x), dy = Math.abs(y - this.origin.y)
    if (this.lock === 'undecided' && Math.max(dx, dy) >= 18) {
      this.lock = dx > dy * 1.5 ? 'horizontal' : 'vertical'
    }
    return this.lock === 'horizontal'
  }
  up(id: number): number {
    const dx = this.last.x - this.origin.x
    const result = this.points.size === 1 && this.lock === 'horizontal' && Math.abs(dx) >= 64 ? (dx < 0 ? 1 : -1) : 0
    this.points.delete(id)
    this.lock = 'blocked'
    return result
  }
  cancel(id: number) { this.points.delete(id); this.lock = 'blocked' }
}

/** Every new intent cancels the old request; only its owner may commit a view. */
export class LatestIntent {
  private revision = 0
  private controller: AbortController | null = null
  begin() {
    this.controller?.abort()
    const controller = this.controller = new AbortController()
    const revision = ++this.revision
    return {signal: controller.signal, current: () => revision === this.revision && !controller.signal.aborted}
  }
  cancel() { this.revision++; this.controller?.abort(); this.controller = null }
}
