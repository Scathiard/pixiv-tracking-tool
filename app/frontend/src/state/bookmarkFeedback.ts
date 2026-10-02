import type { BookmarkState } from '../types'

export interface BookmarkFeedback {
  token: number; until: number; accountId: string; accountEpoch: number
  awaitingReceipt: boolean; operationId: string | null; version: number; settled: boolean
}
interface FeedbackClock {
  now: () => number
  setTimer: (callback: () => void, delay: number) => number
  clearTimer: (timer: number) => void
}

// Visual acknowledgement is shared by work ID, and never changes a bookmark fact.
export function createBookmarkFeedback(clock: FeedbackClock, entries = new Map<number, BookmarkFeedback>()) {
  const timers = new Map<number, number>()
  let nextToken = 0
  function remove(id: number) {
    const timer = timers.get(id)
    if (timer !== undefined) clock.clearTimer(timer)
    timers.delete(id); entries.delete(id)
  }
  function finish(id: number, token: number) {
    const entry = entries.get(id)
    if (!entry || entry.token !== token) return
    entries.set(id, {...entry, settled: true})
    if (clock.now() >= entry.until) remove(id)
  }
  function sameAccount(entry: BookmarkFeedback, state: BookmarkState) {
    return entry.accountId === state.current_account_id && entry.accountEpoch === state.account_epoch
  }
  return {
    active(id: number) { return entries.has(id) },
    start(id: number, state: BookmarkState): number | undefined {
      if (entries.has(id) || !state.current_account_id) return
      const token = ++nextToken
      entries.set(id, {token, until: clock.now() + 3000, accountId: state.current_account_id,
        accountEpoch: state.account_epoch, awaitingReceipt: true, operationId: null,
        version: state.version, settled: false})
      timers.set(id, clock.setTimer(() => {
        const entry = entries.get(id)
        if (entry?.token !== token) return
        timers.delete(id)
        if (entry.settled) entries.delete(id)
      }, 3000))
      return token
    },
    receipt(id: number, token: number, state: BookmarkState | undefined) {
      const entry = entries.get(id)
      if (!entry || entry.token !== token) return
      if (!state) { finish(id, token); return }
      if (state.work_id !== id || !sameAccount(entry, state) || state.version < entry.version) return
      entries.set(id, {...entry, awaitingReceipt: false, operationId: state.operation_id,
        version: state.version})
      if (state.status !== 'pending') finish(id, token)
    },
    observe(id: number, state: BookmarkState) {
      const entry = entries.get(id)
      if (!entry || entry.awaitingReceipt || state.work_id !== id || !sameAccount(entry, state) ||
        entry.operationId !== state.operation_id || state.version < entry.version) return
      if (state.status !== 'pending') finish(id, entry.token)
    },
    clear() {
      for (const id of entries.keys()) remove(id)
    },
  }
}
