import type {GraphActivity} from '../lib/activity';
import type {Point} from './graphGeometry';
export type EffectEdge = {id: string; source: string; target: string; kind: string};
export type ConnectionFlow = {key: string; source: string; target: string; kind: 'file' | 'memory' | 'read' | 'write'; delay: number; duration: number; repeating: false; groupKey: string};
export const MAX_CONNECTION_FLOWS = 12;
/** Build effects exclusively from citations. Activity supplies direction, never topology. */
export function connectionFlows(edges: readonly EffectEdge[], hovered: string | null | undefined, hoverType: string | undefined, activity?: GraphActivity): ConnectionFlow[] {
  const citations = edges.filter(edge => edge.kind === 'source' && edge.source !== edge.target);
  const pairs = new Set(citations.flatMap(edge => [`${edge.source}\0${edge.target}`, `${edge.target}\0${edge.source}`]));
  const result: ConnectionFlow[] = [], used = new Set<string>();
  const add = (flow: ConnectionFlow) => {
    const pair = [flow.source, flow.target].sort().join('\0');
    if (used.has(pair) || result.length >= MAX_CONNECTION_FLOWS) return;
    used.add(pair); result.push(flow);
  };
  if (hovered && (hoverType === 'document' || hoverType === 'memory')) {
    const outgoing = citations.filter(edge => edge.source === hovered || edge.target === hovered).sort((a, b) => a.id.localeCompare(b.id));
    let branch = 0;
    for (const edge of outgoing) {
      const file = hoverType === 'document';
      add({key: `hover:${hovered}:${edge.id}`, groupKey: `hover:${hovered}`, source: hovered, target: edge.source === hovered ? edge.target : edge.source, kind: file ? 'file' : 'memory', delay: Math.min(branch++ * .035, .21), duration: file ? 1.2 : .95, repeating: false});
    }
  }
  const branches = new Map<string, number>();
  for (const link of [...(activity?.links || [])].sort((a, b) => b.token - a.token || a.source.localeCompare(b.source) || a.target.localeCompare(b.target))) {
    if (!pairs.has(`${link.source}\0${link.target}`)) continue;
    const groupKey = `live:${link.token}:${link.source}:${link.kind}`;
    const branch = branches.get(groupKey) || 0;
    branches.set(groupKey, branch + 1);
    add({...link, groupKey, key: `live:${link.token}:${link.source}:${link.target}:${link.kind}`, delay: Math.min(branch * .035, .21), duration: link.kind === 'write' ? 1.3 : 1.05, repeating: false});
  }
  return result;
}
/** One source ignition per encounter/event, regardless of its number of branches. */
export function connectionIgnitions(flows: readonly ConnectionFlow[]): ConnectionFlow[] {
  const seen = new Set<string>();
  return flows.filter(flow => {if (seen.has(flow.groupKey)) return false; seen.add(flow.groupKey); return true;});
}
export function validFlowPoints(flow: ConnectionFlow, positions: ReadonlyMap<string, Point>): {from: Point; to: Point} | null {
  const from = positions.get(flow.source), to = positions.get(flow.target);
  if (!from || !to || ![from.x, from.y, to.x, to.y].every(Number.isFinite) || Math.hypot(to.x - from.x, to.y - from.y) < .01) return null;
  return {from, to};
}
