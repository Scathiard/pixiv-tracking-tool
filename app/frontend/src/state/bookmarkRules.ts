export interface VersionedBookmark {
  local_unknown?: boolean
  version: number; status: string; operation_id?: string | null
  account_epoch?: number; current_account_id?: string | null
}
export interface BookmarkFact {
  value: boolean | null; verified: boolean
  fact_account_id: string | null; current_account_id: string | null
}
export function showsBookmarkFact(state: BookmarkFact | undefined): boolean {
  return !!state?.verified && state.value === true && !!state.fact_account_id &&
    (!state.current_account_id || state.fact_account_id === state.current_account_id)
}
export function nextBookmarkIntent(state: BookmarkFact): boolean {
  return !(state.verified && state.value === true &&
    state.fact_account_id === state.current_account_id)
}
export function acceptsBookmarkUpdate(previous: VersionedBookmark | undefined, incoming: VersionedBookmark) {
  if (!previous) return true
  const oldEpoch = previous.account_epoch ?? 0, newEpoch = incoming.account_epoch ?? 0
  if (newEpoch < oldEpoch) return false
  if (newEpoch > oldEpoch) return true
  if (previous.current_account_id !== incoming.current_account_id) return false
  if (!Number.isSafeInteger(incoming.version) || incoming.version < 0 || incoming.version < previous.version) return false
  if (incoming.version > previous.version) return true
  if (previous.operation_id && incoming.operation_id !== previous.operation_id) return false
  if (previous.status !== 'pending' && incoming.status === 'pending' && !previous.local_unknown) return false
  return true
}
