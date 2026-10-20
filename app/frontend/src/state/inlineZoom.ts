export interface InlineTransform {scale: number; x: number; y: number}
export interface InlineFrame {width: number; height: number}
export const INLINE_ZOOM_MAX = 5
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
