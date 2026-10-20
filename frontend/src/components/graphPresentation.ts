/** Detail is relative to the fitted board, so a larger library does not make
 * users zoom twenty times farther before records acquire a name. */
export function recordPresentation(relativeZoom: number, effectiveScale: number) {
  const card = relativeZoom >= 1.5;
  const full = relativeZoom >= 2.6;
  return {
    label: relativeZoom >= 1.12 && !card,
    card,
    full,
    metadata: relativeZoom >= 4.2,
    titleSize: Math.min(34, Math.max(28, 13 / Math.max(.000001, effectiveScale))),
    copySize: Math.min(30, Math.max(21, 10.5 / Math.max(.01, effectiveScale))),
  };
}
