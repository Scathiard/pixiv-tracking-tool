export interface CardMedia {
  thumbnail: string | null
  preview: string | null
  animation?: boolean
}

/** Only visible, foreground cards may own original/animation media. */
export function galleryMediaSource(media: CardMedia, active: boolean, thumbnailFailed: boolean, previewFailed: boolean) {
  if (media.animation && media.preview && active && !previewFailed) return {url: media.preview, kind: 'animation'}
  if (media.thumbnail && !thumbnailFailed) return {url: media.thumbnail, kind: 'thumbnail'}
  if (media.preview && active && !previewFailed) return {url: media.preview, kind: media.animation ? 'animation' : 'original'}
  return null
}

/** Unchanged metadata must not turn an image error into an automatic retry loop. */
export function newerMediaRevision(incoming: number | undefined, previous: number | undefined) {
  return Number.isSafeInteger(incoming) && Number.isSafeInteger(previous) && incoming! > previous!
}

export const MEDIA_RETRY_MS = 12000
export function mediaRetryDelay(failedAt: number, now: number) {
  return Math.max(0, MEDIA_RETRY_MS - (now - failedAt))
}
