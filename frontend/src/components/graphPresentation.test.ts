import {describe, expect, it} from 'vitest';
import {recordPresentation} from './graphPresentation';

describe('early record detail', () => {
  it('reveals names and cards at the same relative zoom for differently sized libraries', () => {
    for (const fit of [.03, .1, .3, .8]) {
      expect(recordPresentation(1, fit).card).toBe(false);
      expect(recordPresentation(1.2, fit * 1.2).label).toBe(true);
      expect(recordPresentation(1.5, fit * 1.5).card).toBe(true);
      expect(recordPresentation(2.6, fit * 2.6).full).toBe(true);
    }
  });
  it('bounds titles to fit the card at every fitted scale', () => {
    for (const scale of [.00001, .01, .1, 1, 100]) {
      const style = recordPresentation(3, scale);
      expect(style.titleSize).toBeGreaterThanOrEqual(28);
      expect(style.titleSize).toBeLessThanOrEqual(34);
      expect(style.copySize).toBeGreaterThanOrEqual(21);
      expect(style.copySize).toBeLessThanOrEqual(30);
    }
  });
});
