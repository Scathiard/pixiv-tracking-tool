// One recognizer owns single/double taps. Scroll, long press and multi-touch
// cancel the pending click instead of accidentally opening or bookmarking.
export class ImageTaps {
  private points = new Map<number, {x: number; y: number; time: number}>()
  private blocked = false
  private previous: {x: number; y: number; time: number} | null = null
  private timer: ReturnType<typeof setTimeout> | undefined
  private single: () => void
  private double: () => void
  private now: () => number
  constructor(single: () => void, double: () => void, now = () => performance.now()) {
    this.single = single; this.double = double; this.now = now
  }
  down(id: number, x: number, y: number) {
    clearTimeout(this.timer)
    if (!this.points.size) this.blocked = false
    this.points.set(id, {x, y, time: this.now()})
    if (this.points.size > 1) this.cancel()
  }
  move(id: number, x: number, y: number) {
    const point = this.points.get(id)
    if (point && Math.hypot(point.x - x, point.y - y) > 8) this.cancel()
  }
  up(id: number, x: number, y: number) {
    const point = this.points.get(id)
    this.points.delete(id)
    if (!point || this.blocked || this.now() - point.time > 500) return
    const now = this.now()
    if (this.previous && now - this.previous.time < 300 && Math.hypot(x - this.previous.x, y - this.previous.y) < 24) {
      clearTimeout(this.timer); this.previous = null; this.double()
    } else {
      clearTimeout(this.timer)
      this.previous = {x, y, time: now}
      this.timer = setTimeout(() => { this.previous = null; if (!this.blocked) this.single() }, 300)
    }
  }
  cancel() { this.blocked = true; this.previous = null; clearTimeout(this.timer) }
  cancelPointer(id: number) { this.points.delete(id); this.cancel() }
}
