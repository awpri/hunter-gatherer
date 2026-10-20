import {describe, expect, it} from 'vitest';
import {connectionFlows, connectionIgnitions, MAX_CONNECTION_FLOWS, validFlowPoints} from './connectionFlowModel';
const source = (id: string, a: string, b: string) => ({id, source: a, target: b, kind: 'source'});
describe('citation energy flow', () => {
  it('follows hover outwards and ignores topic associations', () => {
    const flows = connectionFlows([source('a', 'memory', 'file'), {id: 'topic', source: 'file', target: 'unrelated', kind: 'topic'}], 'file', 'document');
    expect(flows).toHaveLength(1);
    expect(flows[0]).toMatchObject({source: 'file', target: 'memory', kind: 'file', repeating: false});
    expect(connectionFlows([source('a', 'memory', 'file')], 'memory', 'memory')[0]).toMatchObject({source: 'memory', target: 'file', repeating: false});
  });
  it('preserves real activity direction but rejects fabricated links', () => {
    const links = [{source: 'file', target: 'memory', kind: 'read' as const, token: 3}, {source: 'file', target: 'fake', kind: 'write' as const, token: 4}];
    const flows = connectionFlows([source('a', 'memory', 'file')], null, undefined, {nodes: {}, links});
    expect(flows).toHaveLength(1);
    expect(flows[0]).toMatchObject({source: 'file', target: 'memory', kind: 'read'});
  });
  it('bounds dense fans and retains stable identities under reordered input', () => {
    const edges = Array.from({length: 100}, (_, i) => source(String(i), 'file', `m${i}`));
    const flows = connectionFlows(edges, 'file', 'document');
    expect(flows).toHaveLength(MAX_CONNECTION_FLOWS);
    expect(new Set(flows.map(flow => flow.target)).size).toBe(MAX_CONNECTION_FLOWS);
    expect(connectionFlows([...edges].reverse(), 'file', 'document')).toEqual(flows);
    expect(flows.map(flow => flow.delay)).toEqual([...flows.map(flow => flow.delay)].sort((x, y) => x - y));
  });
  it('ignites one source for a coordinated fan and preserves hover identity through activity', () => {
    const edges = [source('a', 'file', 'm1'), source('b', 'file', 'm2')];
    const before = connectionFlows(edges, 'file', 'document');
    const during = connectionFlows(edges, 'file', 'document', {nodes: {}, links: [{source: 'm1', target: 'file', token: 42, kind: 'read'}]});
    expect(during).toEqual(before);
    expect(before.every(flow => !flow.repeating)).toBe(true);
    expect(connectionIgnitions(before)).toHaveLength(1);
    expect(Math.max(...before.map(flow => flow.delay + flow.duration))).toBeLessThanOrEqual(1.41);
  });
  it('groups agent branches per event and source without repeating a trajectory', () => {
    const edges = [source('a', 'file', 'm1'), source('b', 'file', 'm2')];
    const links = ['m1', 'm2'].map(target => ({source: 'file', target, token: 7, kind: 'write' as const}));
    const flows = connectionFlows(edges, null, undefined, {nodes: {}, links});
    expect(flows).toHaveLength(2);
    expect(connectionIgnitions(flows)).toHaveLength(1);
    expect(flows.every(flow => !flow.repeating)).toBe(true);
  });
  it('rejects missing, invalid, and coincident endpoints', () => {
    const flow = connectionFlows([source('a', 'm', 'f')], 'm', 'memory')[0];
    expect(validFlowPoints(flow, new Map())).toBeNull();
    expect(validFlowPoints(flow, new Map([['m', {x: 0, y: 0}], ['f', {x: Infinity, y: 1}]]))).toBeNull();
    expect(validFlowPoints(flow, new Map([['m', {x: 0, y: 0}], ['f', {x: 0, y: 0}]]))).toBeNull();
    expect(validFlowPoints(flow, new Map([['m', {x: 0, y: 0}], ['f', {x: 3, y: 4}]]))).toEqual({from: {x: 0, y: 0}, to: {x: 3, y: 4}});
  });
});
