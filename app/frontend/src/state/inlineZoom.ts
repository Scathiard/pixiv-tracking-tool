export interface InlineTransform {scale: number; x: number; y: number}
export interface InlineFrame {width: number; height: number}
export const INLINE_ZOOM_MAX = 5
export const INLINE_ZOOM_DURATION = 180
/** Sample the short double-tap motion; direct gestures use the sampled value immediately. */
export function inlineZoomFrame(from: InlineTransform, to: InlineTransform, elapsed: number): InlineTransform {
  const progress = Math.max(0, Math.min(1, elapsed / INLINE_ZOOM_DURATION))
  const eased = 1 - (1 - progress) ** 3
  return {scale: from.scale + (to.scale - from.scale) * eased,
    x: from.x + (to.x - from.x) * eased, y: from.y + (to.y - from.y) * eased}
}
export function boundInlineTransform(value: InlineTransform, frame: InlineFrame): InlineTransform {
  const scale = Math.max(1, Math.min(INLINE_ZOOM_MAX, value.scale))
  if (scale <= 1.005) return {scale: 1, x: 0, y: 0}
  return {scale, x: Math.max(frame.width * (1 - scale), Math.min(0, value.x)),
    y: Math.max(frame.height * (1 - scale), Math.min(0, value.y))}
}
/** Keep the image point under the pointer fixed, then contain the enlarged image. */
export function inlineZoomAt(value: InlineTransform, scale: number, point: {x: number; y: number}, frame: InlineFrame): InlineTransform {
  scale = Math.max(1, Math.min(INLINE_ZOOM_MAX, scale))
  return boundInlineTransform({scale, x: point.x - (point.x - value.x) * scale / value.scale,
    y: point.y - (point.y - value.y) * scale / value.scale}, frame)
}
