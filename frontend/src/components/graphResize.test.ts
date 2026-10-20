import {describe, expect, it} from 'vitest';
import {ResizePreviewLedger, clampResizeSize} from './graphResize';

describe('optimistic group resizing', () => {
  it('does not let an older completion clear a newer drag or another group', () => {
    const ledger = new ResizePreviewLedger();
    const first = ledger.begin('A', {width: 600, height: 500});
    const other = ledger.begin('B', {width: 800, height: 700});
    const latest = ledger.begin('A', {width: 900, height: 600});
    expect(ledger.settle('A', first)).toBe(false);
    expect(ledger.snapshot()).toEqual({A: {width: 900, height: 600}, B: {width: 800, height: 700}});
    expect(ledger.settle('B', other)).toBe(true);
    expect(ledger.snapshot()).toEqual({A: {width: 900, height: 600}});
    expect(ledger.settle('A', latest)).toBe(true);
    expect(ledger.snapshot()).toEqual({});
  });
  it('rolls a cancelled or failed preview back and rejects updates from its abandoned pointer', () => {
    const ledger = new ResizePreviewLedger(), token = ledger.begin('A', {width: 600, height: 500});
    expect(ledger.update('A', token, {width: 1200, height: 700})).toBe(true);
    expect(ledger.settle('A', token)).toBe(true);
    expect(ledger.update('A', token, {width: 1400, height: 900})).toBe(false);
    expect(ledger.snapshot()).toEqual({});
  });
  it('bounds server requests even when the rendered collision minimum is larger', () => {
    expect(clampResizeSize({width: 5000, height: 20000})).toEqual({width: 4000, height: 16000});
    expect(clampResizeSize({width: 5, height: 10})).toEqual({width: 540, height: 300});
    expect(clampResizeSize({width: 5000, height: 20000}, {width: 7000, height: 30000})).toEqual({width: 4000, height: 16000});
    expect(clampResizeSize({width: NaN, height: Infinity})).toEqual({width: 540, height: 300});
  });
});
