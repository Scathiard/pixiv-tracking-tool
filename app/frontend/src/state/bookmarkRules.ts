export interface VersionedBookmark {version: number; status: string; operation_id?: string | null}
export function acceptsBookmarkUpdate(previous: VersionedBookmark | undefined, incoming: VersionedBookmark) {
  if (!previous) return true
  if (!Number.isSafeInteger(incoming.version) || incoming.version < 0 || incoming.version < previous.version) return false
  if (incoming.version > previous.version) return true
  if (previous.operation_id && incoming.operation_id !== previous.operation_id) return false
  if (previous.status !== 'pending' && incoming.status === 'pending') return false
  return true
}
