import {describe, expect, it} from 'vitest';
import {agentFollowCamera} from './graphAgentFollow';
const size = {width: 1000, height: 700};
const point = (x: number, y: number, recordScale = 1) => ({x, y, recordScale});
describe('automatic activity follow', () => {
  it('preserves visible activity and empty batches exactly', () => {
    const view = {x: 0, y: 0, scale: .2};
    expect(agentFollowCamera(view, size, [point(2500, 1750)], .2)).toBe(view);
    expect(agentFollowCamera(view, size, [], .2)).toBe(view);
    const detailed = {...view, scale: 1};
    expect(agentFollowCamera(detailed, size, [point(500, 350)], .2)).toBe(detailed);
  });
  it('pans offscreen activity minimally without zooming into an overview', () => {
    const next = agentFollowCamera({x: 0, y: 0, scale: .2}, size, [point(6000, 1750)], .2);
    expect(next.scale).toBe(.2);
    expect(next.x).toBeCloseTo(904 - (6000 + 148) * .2);
    expect(next.y).toBe(0);
  });
  it('caps offscreen following at contextual scale instead of card detail', () => {
    const next = agentFollowCamera({x: 0, y: 0, scale: 2}, size, [point(6000, 1750)], .2);
    expect(next.scale).toBeCloseTo(.3);
  });
  it('fits separated activity with scaled cards and surrounding margins', () => {
    const points = [point(-5000, -2000, 3), point(5000, 2000, 2)];
    const next = agentFollowCamera({x: 0, y: 0, scale: .2}, size, points, .2);
    expect(next.scale).toBeLessThan(.2);
    for (const p of points) {
      expect(next.x + (p.x - 148 * p.recordScale) * next.scale).toBeGreaterThanOrEqual(96 - 1e-8);
      expect(next.x + (p.x + 148 * p.recordScale) * next.scale).toBeLessThanOrEqual(904 + 1e-8);
      expect(next.y + (p.y - 112 * p.recordScale) * next.scale).toBeGreaterThanOrEqual(92 - 1e-8);
      expect(next.y + (p.y + 112 * p.recordScale) * next.scale).toBeLessThanOrEqual(588 + 1e-8);
    }
  });
  it('never zooms in across overview scales or small viewports', () => {
    for (const scale of [.01, .1, .5, 2]) {
      const next = agentFollowCamera({x: 0, y: 0, scale}, {width: 280, height: 200}, [point(10000, -10000, 4)], .1);
      expect(next.scale).toBeLessThanOrEqual(scale);
      expect(next.scale).toBeLessThanOrEqual(.15 + 1e-8);
      expect(Number.isFinite(next.x) && Number.isFinite(next.y)).toBe(true);
    }
  });
});
