import { beforeAll, describe, expect, it, vi } from 'vitest';

let preferences: typeof import('./appearance');
const storage = new Map<string, string>();
const dataset: Record<string, string> = {};
beforeAll(async () => {
  vi.stubGlobal('localStorage', { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  vi.stubGlobal('document', { documentElement: { dataset }, querySelector: () => null });
  vi.stubGlobal('window', { addEventListener: vi.fn() });
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('reduced-motion'), addEventListener: vi.fn() }));
  preferences = await import('./appearance');
});
describe('appearance preferences', () => {
  it('accepts only supported values and fills defaults', () => {
    expect(preferences.validateAppearance({ theme: 'neon', accent: 'rose', density: 5, motion: 'reduced', extra: 'ignored' })).toEqual({ theme: 'dark', accent: 'rose', density: 'comfortable', motion: 'reduced', surface: 'graphite', lightSurface:'paper', backdrop: 'dots' });
    expect(preferences.validateAppearance(null)).toEqual(preferences.DEFAULT_APPEARANCE);
    expect(preferences.validateAppearance('dark')).toEqual(preferences.DEFAULT_APPEARANCE);
  });
  it('persists changes and honors device reduced motion even in system mode', () => {
    preferences.setAppearance({ theme: 'light', accent: 'blue', density: 'compact', motion: 'system' });
    expect(dataset).toEqual({ theme: 'light', accent: 'blue', density: 'compact', motion: 'reduced', surface: 'graphite', lightSurface:'paper', backdrop: 'dots' });
    expect(JSON.parse(storage.get(preferences.APPEARANCE_KEY)!)).toEqual({ theme: 'light', accent: 'blue', density: 'compact', motion: 'system', surface: 'graphite', lightSurface:'paper', backdrop: 'dots' });
  });
  it('migrates v1 settings and rejects unsupported additions', () => {
    expect(preferences.validateAppearance({ theme: 'light', accent: 'amber' })).toMatchObject({ theme: 'light', accent: 'amber', surface: 'graphite', backdrop: 'dots' });
    expect(preferences.validateAppearance({ surface: 'invalid', lightSurface:'invalid', backdrop: 'stars' })).toEqual(preferences.DEFAULT_APPEARANCE);
  });
  it('applies presets while preserving motion and density', () => {
    preferences.setAppearance({ density: 'compact', motion: 'reduced' });
    preferences.applyAppearancePreset('midnight');
    expect(dataset).toMatchObject({ theme: 'dark', accent: 'blue', surface: 'midnight', backdrop: 'grid', density: 'compact', motion: 'reduced' });
    const saved = storage.get(preferences.APPEARANCE_KEY);
    preferences.applyAppearancePreset('unknown');
    expect(storage.get(preferences.APPEARANCE_KEY)).toBe(saved);
    preferences.applyAppearancePreset('paper');
    expect(dataset).toMatchObject({ theme: 'light', accent: 'rose', surface: 'midnight', lightSurface:'paper', backdrop: 'dots' });
  });
  it('preserves separate dark and light surfaces through presets and system mode', () => {
    preferences.applyAppearancePreset('carbon');
    preferences.applyAppearancePreset('sand');
    preferences.setAppearance({theme:'system',accent:'violet'});
    const saved=JSON.parse(storage.get(preferences.APPEARANCE_KEY)!);
    expect(saved).toMatchObject({theme:'system',surface:'carbon',lightSurface:'sand',accent:'violet',motion:'reduced',density:'compact'});
    expect(preferences.validateAppearance(saved)).toEqual(saved);
    preferences.setAppearance({theme:'dark'});
    expect(dataset).toMatchObject({theme:'dark',surface:'carbon',lightSurface:'sand',accent:'violet'});
  });
  it('keeps appearance usable if persistence is blocked', () => {
    vi.stubGlobal('localStorage', { setItem: () => { throw new Error('unavailable'); } });
    expect(() => preferences.resetAppearance()).not.toThrow();
    expect(dataset.theme).toBe('dark');
    expect(dataset.accent).toBe('mint');
  });
});
