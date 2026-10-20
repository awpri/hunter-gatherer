import {describe, it, expect} from 'vitest';
import {backdropMetrics} from './GraphBackdrop';

describe('world backdrop', () => {
  it('scales continuously with the camera and disappears when zoomed out', () => {
    const far = backdropMetrics({x: 0, y: 0, scale: .04}, .1);
    const overview = backdropMetrics({x: 0, y: 0, scale: .1}, .1);
    const close = backdropMetrics({x: 70, y: -20, scale: .2}, .1);
    expect(far.opacity).toBe(0);
    expect(overview.opacity).toBeLessThan(.08);
    expect(close.opacity).toBeGreaterThan(.4);
    expect(close.spacing).toBe(overview.spacing * 2);
    expect(backdropMetrics({x: 1000, y: -1000, scale: .1}, .1)).toEqual(overview);
  });
});
