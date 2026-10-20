export type Point = {x: number; y: number};
export type CameraView = {x: number; y: number; scale: number};
export type CanvasSize = {width: number; height: number};

/** Reversing a citation preserves its curve, so pulses can travel either way. */
export function sourceCurve(from: Point, to: Point, bendFraction = .09, bendLimit = 36): string {
  const dx = to.x - from.x, dy = to.y - from.y, length = Math.hypot(dx, dy);
  if (!length) return `M ${from.x} ${from.y} L ${to.x} ${to.y}`;
  const direction = from.x < to.x || (from.x === to.x && from.y < to.y) ? 1 : -1;
  const bend = Math.min(bendLimit, length * bendFraction) * direction;
  return `M ${from.x} ${from.y} Q ${(from.x + to.x) / 2 - dy / length * bend} ${(from.y + to.y) / 2 + dx / length * bend} ${to.x} ${to.y}`;
}
export function neighborhood(selected: string | null | undefined, edges: readonly {source: string; target: string}[]): Set<string> {
  const ids = new Set<string>();
  if (!selected) return ids;
  ids.add(selected);
  for (const edge of edges) {
    if (edge.source === selected) ids.add(edge.target);
    if (edge.target === selected) ids.add(edge.source);
  }
  return ids;
}
export function viewportBounds(view: CameraView, size: CanvasSize) {
  return {x: -view.x / view.scale, y: -view.y / view.scale, width: size.width / view.scale, height: size.height / view.scale};
}

/** Details are additive. The group summary never fades or disappears. Reveal
 * record copy only when its world-space type reaches ten screen pixels. */
export function semanticLayers(scale: number, _fitScale = 1) {
  const t = Math.max(0, Math.min(1, (scale - .48) / .35));
  const detail = t * t * (3 - 2 * t);
  return {detail, preview: 1, nodeSize: 22, records: scale >= .68, descriptions: scale >= .68, metadata: scale >= 1};
}
export function zoomAt(view: CameraView, factor: number, point: Point, minScale = .02, maxScale = 4): CameraView {
  const scale = Math.max(minScale, Math.min(maxScale, view.scale * factor)), ratio = scale / view.scale;
  return {x: point.x - (point.x - view.x) * ratio, y: point.y - (point.y - view.y) * ratio, scale};
}

/** A real fit includes the entire world, with room for controls. It never
 * substitutes an arbitrary minimum card size that sends groups offscreen. */
export function fitWorld(bounds: {x: number; y: number; width: number; height: number}, size: CanvasSize, maximum = 1.2): CameraView {
  const width = Math.max(1, size.width - 42), height = Math.max(1, size.height - 86);
  const scale = Math.max(.000001, Math.min(maximum, width / Math.max(1, bounds.width), height / Math.max(1, bounds.height)));
  return {scale, x: size.width / 2 - (bounds.x + bounds.width / 2) * scale, y: 22 + height / 2 - (bounds.y + bounds.height / 2) * scale};
}

/** Three seeded motions share a bounded envelope; no accumulated layout force
 * can push a card beyond its reserved slot. IDs retain their motion identity. */
export function driftPoint(point: Point, id: string, seconds: number, amplitude = 9): Point {
  let seed = 0;
  for (const char of id) seed = (seed * 31 + char.charCodeAt(0)) >>> 0;
  const phase = seed % 628 / 100;
  const mode = seed % 3;
  const x = mode === 0 ? Math.sin(seconds * .65 + phase) : mode === 1 ? Math.sin(seconds * .4 + phase) * Math.cos(seconds * .19) : Math.sin(seconds * .54 + phase);
  const y = mode === 2 ? Math.sin(seconds * .83 + phase) * .5 : Math.cos(seconds * .47 + phase) * .65;
  return {x: point.x + x * amplitude, y: point.y + y * amplitude};
}

/** Keyboard focus reveals a whole record card, including its preview. */
export function revealPoint(view: CameraView, size: CanvasSize, point: Point, padding = 45) {
  const x = point.x * view.scale + view.x, y = point.y * view.scale + view.y;
  if (x >= padding && x <= size.width - padding && y >= padding + 35 && y <= size.height - padding - 25) return view;
  return {...view, x: size.width / 2 - point.x * view.scale, y: size.height / 2 - point.y * view.scale};
}

// Kept as a stable geometry utility: content expands in place, rather than
// crossfading and moving through the group's permanent summary.
export function semanticPoint(point: Point, _group: {x: number; y: number; width: number; height: number} | undefined, _detail: number): Point {
  return point;
}

/** A group opens on its record body at readable card scale. Large groups keep
 * their first rows in view and remain pannable instead of shrinking copy. */
export function focusRecordBody(points: readonly Point[], size: CanvasSize, recordScale = 1): CameraView | null {
  if (!points.length) return null;
  const x = Math.min(...points.map(point => point.x)) - 140 * recordScale, y = Math.min(...points.map(point => point.y)) - 112 * recordScale;
  const width = Math.max(...points.map(point => point.x)) + 140 * recordScale - x, height = Math.max(...points.map(point => point.y)) + 112 * recordScale - y;
  const target = fitWorld({x, y, width, height}, size, 1.15 / recordScale);
  const scale = Math.max(.8 / recordScale, target.scale);
  const visibleWidth = (size.width - 42) / scale, visibleHeight = (size.height - 86) / scale;
  return {scale, x: size.width / 2 - (x + Math.min(width, visibleWidth) / 2) * scale, y: 22 + (size.height - 86) / 2 - (y + Math.min(height, visibleHeight) / 2) * scale};
}

/** Typography starts legible in the fitted overview and approaches intrinsic
 * detail type as the camera moves closer. It never changes opacity or bounds. */
export function groupTypography(headerHeight: number, width: number, overviewScale: number, scale: number, constrainWidth = false, contentHeight = headerHeight) {
  const pixels = headerHeight * overviewScale;
  const type = (target: number, minimum: number, factor: number, intrinsic: number, cap: number, column: number) => Math.min(constrainWidth ? width / column : Infinity, Math.max(intrinsic, Math.min(Math.min(target, Math.max(minimum, pixels * factor)) / overviewScale, cap / scale)));
  const result = {title: type(14, 10, .14, 30, 24, 18), copy: type(11, 8, .11, 21, 17, 26), meta: type(9, 7, .09, 17, 12, 32), gap: Math.max(6, Math.min(4 / overviewScale, 4 / scale))};
  // Budget for the taller icon line, title, both margins, and two complete
  // description lines. Narrow panels must not clip the last line mid-letter.
  const occupied = result.meta * 1.62 + result.title * 1.2 + result.gap * 2 + result.copy * 2.8;
  const fit = Math.min(1, Math.max(1, contentHeight - 6) / occupied);
  return {title: result.title * fit, copy: result.copy * fit, meta: result.meta * fit, gap: result.gap * fit};
}
