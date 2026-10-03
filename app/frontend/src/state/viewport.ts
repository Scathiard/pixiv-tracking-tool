/** Browser chrome and keyboards can shrink the visible area without resizing dvh. */
export function visibleViewportBottom(layoutHeight: number, viewport: Pick<VisualViewport, 'height' | 'offsetTop' | 'scale'> | null): number | null {
  if (!Number.isFinite(layoutHeight) || layoutHeight <= 0) return null
  if (!viewport) return layoutHeight
  // Native pinch zoom must magnify the existing layout, not reflow the reader.
  if (Math.abs(viewport.scale - 1) > .01 || !Number.isFinite(viewport.height) || viewport.height <= 0) return null
  return Math.min(layoutHeight, Math.max(0, viewport.offsetTop) + viewport.height)
}

export function observeVisibleViewport(): () => void {
  const root = document.documentElement
  const viewport = window.visualViewport
  let frame = 0
  function update() {
    frame = 0
    const bottom = visibleViewportBottom(window.innerHeight, viewport)
    if (bottom !== null) root.style.setProperty('--visible-viewport-bottom', `${bottom}px`)
  }
  function schedule() {
    if (!frame) frame = window.requestAnimationFrame(update)
  }
  update()
  window.addEventListener('resize', schedule)
  viewport?.addEventListener('resize', schedule)
  viewport?.addEventListener('scroll', schedule)
  document.addEventListener('visibilitychange', schedule)
  return () => {
    window.cancelAnimationFrame(frame)
    window.removeEventListener('resize', schedule)
    viewport?.removeEventListener('resize', schedule)
    viewport?.removeEventListener('scroll', schedule)
    document.removeEventListener('visibilitychange', schedule)
    root.style.removeProperty('--visible-viewport-bottom')
  }
}
