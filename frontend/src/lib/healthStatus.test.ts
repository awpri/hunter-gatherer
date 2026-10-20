import {afterEach, describe, expect, it, vi} from 'vitest';
import {healthLabel, readHealthSummary, saveHealthSummary, summarizeHealth} from './healthStatus';
import type {HealthReport} from './libraryApi';
const report = (codes: string[], counts?: Record<string, number>): HealthReport => ({scope: {memories: 0, documents: 0, chunks: 0, evidence_refs: 0}, issues: codes.map(code => ({code})), counts, warnings: ['Diagnostic consistency does not verify claims or authenticity.']});
describe('health summary', () => {
  afterEach(() => vi.unstubAllGlobals());
  it('uses gray/no previous scan before a manual result', () => {expect(healthLabel(null)).toContain('no previous scan');});
  it('does not treat generic diagnostic caveats as failed checks', () => {expect(summarizeHealth(report([]), false, 1).tone).toBe('green');});
  it('distinguishes review notes from actual integrity failures', () => {
    expect(summarizeHealth(report(['missing_source_date']), false, 1).tone).toBe('amber');
    expect(summarizeHealth(report(['evidence_quote_mismatch']), false, 1).tone).toBe('red');
  });
  it('uses full issue totals when the returned list is capped', () => {
    const value = summarizeHealth(report(['missing_source_date'], {missing_source_date: 100, evidence_quote_mismatch: 1}), false, 1);
    expect(value.tone).toBe('red'); expect(value.issues).toBe(101);
  });
  it('labels scope and historic timestamp honestly', () => {
    expect(healthLabel(summarizeHealth(report([]), true, 1))).toContain('private-inclusive');
    expect(healthLabel(summarizeHealth(report([]), false, 1))).toContain('not a live check');
  });
  it('persists only public summaries, separately for each store', () => {
    const data = new Map<string, string>();
    vi.stubGlobal('localStorage', {getItem: (key: string) => data.get(key) || null, setItem: (key: string, value: string) => data.set(key, value)});
    saveHealthSummary('one', summarizeHealth(report([]), false, 1));
    saveHealthSummary('two', summarizeHealth(report(['evidence_quote_mismatch']), true, 2));
    expect(readHealthSummary('one')?.tone).toBe('green');
    expect(readHealthSummary('two')).toBeNull();
    expect(data.size).toBe(1);
  });
  it('ignores malformed cached summaries', () => {
    vi.stubGlobal('localStorage', {getItem: () => '{broken'});
    expect(readHealthSummary('one')).toBeNull();
  });
});
