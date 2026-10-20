import type {GroupBounds, Point} from './graphLayout';

/** Fixed group rectangles own the cursor. Moving record hit areas never decide
 * which cloud receives the lens. Header and empty body space belong to the group. */
export function groupAtPoint(groups: readonly GroupBounds[], point: Point | null): GroupBounds | undefined {
  if (!point) return undefined;
  return groups.find(group => point.x >= group.x && point.x <= group.x + group.width
    && point.y >= group.y && point.y <= group.y + group.height);
}

export function groupLensAmount(group: GroupBounds | undefined, point: Point | null, cameraScale: number): number {
  if (!group || !point) return 0;
  const inset = Math.min(point.x - group.x, group.x + group.width - point.x,
    point.y - group.y, group.y + group.height - point.y) * cameraScale;
  const t = Math.max(0, Math.min(1, inset / 24));
  return t * t * (3 - 2 * t);
}
