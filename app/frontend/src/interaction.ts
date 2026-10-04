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

/** A view owns its reads; superseded and disposed reads can never commit. */
export class ReadLifecycle {
  private intent = new LatestIntent()
  private flight: {key: string; promise: Promise<void>} | null = null
  private disposed = false
  run<T>(key: string, read: (signal: AbortSignal) => Promise<T>, commit: (value: T) => void,
    fail: (error: unknown) => void, finish: () => void = () => {}) {
    if (this.disposed) return Promise.resolve()
    if (this.flight?.key === key) return this.flight.promise
    const ticket = this.intent.begin()
    const promise = Promise.resolve().then(() => {
      ticket.signal.throwIfAborted()
      return read(ticket.signal)
    }).then(value => { if (ticket.current()) commit(value) }, error => {
      if (ticket.current() && !isAbortError(error)) fail(error)
    }).finally(() => {
      if (ticket.current()) finish()
      if (this.flight?.promise === promise) this.flight = null
    })
    this.flight = {key, promise}
    return promise
  }
  cancel() { this.intent.cancel(); this.flight = null }
  dispose() { this.disposed = true; this.cancel() }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

/** One underlying read, separately owned by every waiting consumer. */
export class SharedReads {
  private flights = new Map<string, {controller: AbortController; promise: Promise<unknown>; consumers: number; completed: boolean}>()
  read<T>(key: string, start: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal | null): Promise<T> {
    if (signal?.aborted) return Promise.reject(signal.reason)
    let flight = this.flights.get(key)
    if (!flight) {
      const controller = new AbortController()
      flight = {controller, consumers: 0, completed: false, promise: Promise.resolve().then(() => {
        controller.signal.throwIfAborted()
        return start(controller.signal)
      })}
      const started = flight
      flight.promise = flight.promise.then(value => { started.completed = true; return value }, error => { started.completed = true; throw error })
      this.flights.set(key, flight)
    }
    const owned = flight
    owned.consumers++
    return new Promise<T>((resolve, reject) => {
      let settled = false
      const release = () => {
        if (settled) return false
        settled = true
        signal?.removeEventListener('abort', abort)
        if (--owned.consumers === 0) {
          if (this.flights.get(key) === owned) this.flights.delete(key)
          if (!owned.completed) owned.controller.abort()
        }
        return true
      }
      const abort = () => { if (release()) reject(signal?.reason) }
      signal?.addEventListener('abort', abort, {once: true})
      owned.promise.then(value => { if (release()) resolve(value as T) }, error => { if (release()) reject(error) })
      if (signal?.aborted) abort()
    })
  }
}
