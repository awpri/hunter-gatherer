import {describe, expect, it} from 'vitest';
import {constellationLayout} from './graphConstellationLayout';
import {layoutGraph, type LayoutInputEdge} from './graphLayout';
const nodes = Array.from({length: 18}, (_, i) => ({id: `node:${i}`, type: 'memory' as const, group_id: `group:${Math.floor(i / 3)}`}));
const edges: LayoutInputEdge[] = [
  {id: 'one', source: 'node:0', target: 'node:3', kind: 'source'},
  {id: 'two', source: 'node:0', target: 'node:6', kind: 'source'},
  {id: 'three', source: 'node:3', target: 'node:9', kind: 'source'},
];
const fixture = () => layoutGraph(nodes, edges, Array.from({length: 6}, (_, i) => ({id: `group:${i}`, section_id: `section:${i % 2}`})), 1.6, {groupSizes: {'group:2': {width: 1400, height: 1000}}});
describe('constellation geometry', () => {
  it('differs from board, preserves saved sizes and local positions without mutating input', () => {
    const base = fixture(), snapshot = structuredClone(base), result = constellationLayout(base, edges);
    expect(base).toEqual(snapshot);
    expect(result.groups).not.toEqual(base.groups);
    expect(result.sections).toEqual([]);
    expect(result.positions.size).toBe(base.positions.size);
    expect(result.omitted).toBe(base.omitted);
    for (const group of result.groups) {
      const original = base.groups.find(other => other.id === group.id)!;
      expect({...group, x: original.x, y: original.y}).toEqual(original);
      for (const id of group.nodeIds) {
        expect(result.positions.get(id)!.x - group.x).toBeCloseTo(base.positions.get(id)!.x - original.x);
        expect(result.positions.get(id)!.y - group.y).toBeCloseTo(base.positions.get(id)!.y - original.y);
      }
    }
    expect(result.groups.find(group => group.id === 'group:2')!.width).toBe(1400);
  });
  it('is deterministic under input reversals and citation direction changes', () => {
    const base = fixture(), result = constellationLayout(base, edges);
    expect(constellationLayout({...base, groups: [...base.groups].reverse(), positions: new Map([...base.positions].reverse())}, [...edges].reverse().map(edge => ({...edge, source: edge.target, target: edge.source})))).toEqual(result);
    expect(constellationLayout(base, edges)).toEqual(result);
  });
  it('keeps finite bounds and separates all unequal rectangles at each viewport aspect', () => {
    for (const aspect of [.4, 1, 3, NaN]) {
      const result = constellationLayout(fixture(), edges, aspect);
      expect(Number.isFinite(result.width) && Number.isFinite(result.height)).toBe(true);
      for (const group of result.groups) {
        expect(group.x).toBeGreaterThanOrEqual(0); expect(group.y).toBeGreaterThanOrEqual(0);
        expect(group.x + group.width).toBeLessThanOrEqual(result.width); expect(group.y + group.height).toBeLessThanOrEqual(result.height);
        for (const other of result.groups) if (other.id !== group.id) expect(group.x + group.width <= other.x || other.x + other.width <= group.x || group.y + group.height <= other.y || other.y + other.height <= group.y).toBe(true);
      }
      for (const point of result.positions.values()) {
        expect(Number.isFinite(point.x) && Number.isFinite(point.y)).toBe(true);
        expect(point.x).toBeGreaterThanOrEqual(0); expect(point.x).toBeLessThanOrEqual(result.width);
        expect(point.y).toBeGreaterThanOrEqual(0); expect(point.y).toBeLessThanOrEqual(result.height);
      }
      expect(result.width * result.height).toBeLessThan(fixture().width * fixture().height * 3);
    }
  });
  it('uses only actual source links and handles empty and single-group inputs', () => {
    const base = fixture();
    const unrelated: LayoutInputEdge[] = [{id: 'topic', source: 'node:0', target: 'node:15', kind: 'topic'}, {id: 'broken', source: 'missing', target: 'node:3', kind: 'source'}, {id: 'internal', source: 'node:0', target: 'node:1', kind: 'source'}];
    expect(constellationLayout(base, unrelated)).toEqual(constellationLayout(base, []));
    expect(constellationLayout(base, edges).groups).not.toEqual(constellationLayout(base, []).groups);
    expect(constellationLayout(layoutGraph([], []), []).groups).toEqual([]);
    expect(constellationLayout(layoutGraph(nodes.slice(0, 3), []), []).positions.size).toBe(3);
  });
});
