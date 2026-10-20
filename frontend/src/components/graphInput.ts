export type WheelInput = {
  deltaX: number; deltaY: number; deltaMode: number;
  ctrlKey: boolean; metaKey: boolean; shiftKey: boolean;
};
export type WheelGesture = {kind: 'pan'; dx: number; dy: number} | {kind: 'zoom'; factor: number};

/** Scroll zooms; Shift explicitly switches ordinary scrolling to two-axis pan.
 * Native browser pinch arrives as Ctrl+wheel and always remains a zoom gesture. */
export function wheelGesture(event: WheelInput, pageHeight = 800): WheelGesture {
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? Math.max(1, pageHeight) : 1;
  const dx = Number.isFinite(event.deltaX) ? event.deltaX * unit : 0;
  const dy = Number.isFinite(event.deltaY) ? event.deltaY * unit : 0;
  if (event.shiftKey && !event.ctrlKey && !event.metaKey) {
    return {kind: 'pan', dx: -dx, dy: -dy};
  }
  const delta = dy || dx;
  return {kind: 'zoom', factor: Math.exp(-Math.max(-100, Math.min(100, delta)) * .003)};
}
