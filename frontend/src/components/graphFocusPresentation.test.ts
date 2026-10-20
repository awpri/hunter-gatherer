import {describe, expect, it} from 'vitest';
import {graphFocusPresentation, viewportFocusDistance} from './graphFocusPresentation';
const detail = (zoom: number, distance: number, max = 1000) => graphFocusPresentation({relativeZoom: zoom, screenDistance: distance, maxScreenDistance: max});
describe('viewport detail spread', () => {
  it('reveals near records at 130% and reaches all visible records at 150%', () => {
    expect(detail(1.3, 0).detailReveal).toBeCloseTo(1);
    expect(detail(1.3, 1000).detailReveal).toBe(0);
    expect(detail(1.4, 500).detailReveal).toBeCloseTo(1);
    for (const distance of [0, 250, 500, 750, 1000]) expect(detail(1.5, distance).detailReveal).toBeCloseTo(1);
  });
  it('has a smooth partial reveal without depending on world scale', () => {
    expect(detail(1.28, 0).detailReveal).toBeCloseTo(.5);
    expect(detail(1.38, 500).detailReveal).toBeCloseTo(.5);
    expect(detail(1.38, 50, 100).detailReveal).toBeCloseTo(.5);
    for (const zoom of [2, 4, 20]) expect(detail(zoom, 1000).detailReveal).toBe(1);
  });
  it('uses the farthest viewport corner', () => {
    expect(viewportFocusDistance(800, 600, 400, 300)).toBe(500);
    expect(viewportFocusDistance(800, 600, 0, 0)).toBe(1000);
  });
  it('bounds gentle cursor magnification', () => {
    for (const amount of [-1, 0, .5, 1, 2]) for (const distance of [0, 140, 280, 1000]) {
      const result = graphFocusPresentation({relativeZoom: 1.4, screenDistance: distance, maxScreenDistance: 1000, lensAmount: amount});
      expect(result.magnification).toBeGreaterThanOrEqual(1);
      expect(result.magnification).toBeLessThanOrEqual(1.08);
    }
    expect(graphFocusPresentation({relativeZoom: 2, screenDistance: 280, maxScreenDistance: 1000, lensAmount: 1}).magnification).toBe(1);
  });
});
