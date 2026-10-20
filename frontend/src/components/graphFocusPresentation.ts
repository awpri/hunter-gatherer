const clamp = (value: number) => Math.min(1, Math.max(0, value));
const smoothstep = (value: number) => {const t = clamp(value); return t * t * (3 - 2 * t);};

/** Farthest visible point from the focus, in CSS pixels. */
export function viewportFocusDistance(width: number, height: number, focusX: number, focusY: number) {
  return Math.hypot(Math.max(Math.abs(focusX), Math.abs(width - focusX)),
    Math.max(Math.abs(focusY), Math.abs(height - focusY)));
}

/** Detail spreads across the viewport between 130% and 150%. Screen distances
 * keep this behavior independent of the fitted scale or the library's size. */
export function graphFocusPresentation({relativeZoom, screenDistance, maxScreenDistance, lensAmount = 0, lensRadius = 280}: {
  relativeZoom: number;
  screenDistance: number;
  maxScreenDistance: number;
  lensAmount?: number;
  lensRadius?: number;
}) {
  const distance = Math.max(0, screenDistance);
  const normalizedDistance = clamp(distance / Math.max(1, maxScreenDistance));
  const focusWeight = 1 - smoothstep(distance / Math.max(1, lensRadius));
  const magnification = 1 + .08 * clamp(lensAmount) * focusWeight;
  // The transition ends at 1.30 near the focus and 1.50 at the farthest corner.
  const detailReveal = smoothstep((relativeZoom - (1.26 + .2 * normalizedDistance)) / .04);
  return {detailReveal, localZoom: relativeZoom * magnification, magnification, focusWeight};
}
