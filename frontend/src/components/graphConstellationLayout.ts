import {groupSourceLinks, type GroupBounds, type LayoutInputEdge, type LayoutResult} from './graphLayout';

/** Static spatial clusters: citation-connected groups grow outward from their
 * neighbors, with isolated groups placed around the network's perimeter.
 * Board sections are deliberately omitted: their rectangular shelf bounds no
 * longer describe this arrangement. All local card geometry remains intact. */
export function constellationLayout(base: LayoutResult, edges: readonly LayoutInputEdge[], aspect = 1.6): LayoutResult {
  if (!base.groups.length) return {...base, sections: [], positions: new Map(base.positions), groups: []};
  const ratio = Number.isFinite(aspect) ? Math.max(.4, Math.min(3, aspect)) : 1.6;
  const sorted = [...base.groups].sort((a, b) => a.id.localeCompare(b.id));
  const neighbors = new Map(sorted.map(group => [group.id, new Map<string, number>()]));
  for (const link of groupSourceLinks(sorted, edges)) {
    neighbors.get(link.source)!.set(link.target, link.count);
    neighbors.get(link.target)!.set(link.source, link.count);
  }
  const degree = (id: string) => [...neighbors.get(id)!.values()].reduce((sum, count) => sum + count, 0);
  const gap = Math.max(46, Math.min(...sorted.map(group => Math.min(group.width, group.height))) * .09);
  const placed: GroupBounds[] = [];
  const remaining = [...sorted];
  const center = (group: GroupBounds) => ({x: group.x + group.width / 2, y: group.y + group.height / 2});
  let left = 0, top = 0, right = 0, bottom = 0;
  while (remaining.length) {
    // Traverse the real source network before placing disconnected clusters.
    remaining.sort((a, b) => {
      const attached = (id: string) => placed.reduce((sum, group) => sum + (neighbors.get(id)!.get(group.id) || 0), 0);
      return attached(b.id) - attached(a.id) || degree(b.id) - degree(a.id) || a.id.localeCompare(b.id);
    });
    const group = remaining.shift()!;
    if (!placed.length) {
      placed.push({...group, x: -group.width / 2, y: -group.height / 2});
      left = -group.width / 2; right = group.width / 2; top = -group.height / 2; bottom = group.height / 2;
      continue;
    }
    const linked = placed.filter(other => neighbors.get(group.id)!.has(other.id));
    const total = linked.reduce((sum, other) => sum + neighbors.get(group.id)!.get(other.id)!, 0);
    const target = linked.length ? linked.reduce((sum, other) => {
      const point = center(other), weight = neighbors.get(group.id)!.get(other.id)! / total;
      return {x: sum.x + point.x * weight, y: sum.y + point.y * weight};
    }, {x: 0, y: 0}) : {x: 0, y: 0};
    // Touching-boundary candidates per anchor give irregular spatial
    // placement without unbounded force iterations or a live simulation.
    const anchors = [...placed].sort((a, b) => {
      const distance = (other: GroupBounds) => {const point = center(other); return (point.x - target.x) ** 2 + (point.y - target.y) ** 2;};
      return distance(a) - distance(b) || a.id.localeCompare(b.id);
    }).slice(0, 24);
    let best: {x: number; y: number; score: number} | undefined;
    const candidates: {x: number; y: number}[] = [];
    for (const anchor of anchors) {
      const point = center(anchor);
      for (const y of [anchor.y, point.y - group.height / 2, anchor.y + anchor.height - group.height]) {
        candidates.push({x: anchor.x - gap - group.width, y}, {x: anchor.x + anchor.width + gap, y});
      }
      for (const x of [anchor.x, point.x - group.width / 2, anchor.x + anchor.width - group.width]) {
        candidates.push({x, y: anchor.y - gap - group.height}, {x, y: anchor.y + anchor.height + gap});
      }
    }
    // Guaranteed valid fallback, even with highly unequal user dimensions.
    candidates.push({x: right + gap, y: -group.height / 2}, {x: -group.width / 2, y: bottom + gap});
    for (const candidate of candidates) {
      // Isolated groups stay outside the existing network envelope.
      if (!degree(group.id) && !(candidate.x + group.width <= left - gap + .001 || candidate.x >= right + gap - .001 || candidate.y + group.height <= top - gap + .001 || candidate.y >= bottom + gap - .001)) continue;
      if (placed.some(other => candidate.x < other.x + other.width + gap - .001 && candidate.x + group.width + gap > other.x + .001 && candidate.y < other.y + other.height + gap - .001 && candidate.y + group.height + gap > other.y + .001)) continue;
      const width = Math.max(right, candidate.x + group.width) - Math.min(left, candidate.x);
      const height = Math.max(bottom, candidate.y + group.height) - Math.min(top, candidate.y);
      const cx = candidate.x + group.width / 2, cy = candidate.y + group.height / 2;
      const distance = Math.hypot((cx - target.x) / Math.sqrt(ratio), (cy - target.y) * Math.sqrt(ratio));
      const extent = Math.max(width / ratio, height);
      const score = extent + distance * (linked.length ? .55 : .12) + width * height / (base.width + base.height) * .08;
      if (!best || score < best.score) best = {...candidate, score};
    }
    placed.push({...group, x: best!.x, y: best!.y});
    left = Math.min(left, best!.x); top = Math.min(top, best!.y);
    right = Math.max(right, best!.x + group.width); bottom = Math.max(bottom, best!.y + group.height);
  }
  const positions = new Map(base.positions);
  const originals = new Map(sorted.map(group => [group.id, group]));
  const groups = placed.map(group => {
    const original = originals.get(group.id)!;
    const x = group.x - left + 32, y = group.y - top + 32;
    for (const id of group.nodeIds) {
      const point = base.positions.get(id);
      if (point) positions.set(id, {x: point.x + x - original.x, y: point.y + y - original.y});
    }
    return {...group, x, y, nodeIds: [...group.nodeIds], minimumSize: {...group.minimumSize}};
  }).sort((a, b) => a.id.localeCompare(b.id));
  return {positions: new Map([...positions].sort(([a], [b]) => a.localeCompare(b))), groups, sections: [], width: right - left + 64, height: bottom - top + 64, omitted: base.omitted};
}
