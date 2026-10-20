import { useSyncExternalStore } from 'react';

export type Appearance = {
  theme: 'dark' | 'light' | 'system';
  accent: 'mint' | 'blue' | 'amber' | 'rose' | 'neutral' | 'violet' | 'cyan' | 'orange';
  density: 'comfortable' | 'compact';
  motion: 'system' | 'reduced';
  surface: 'graphite' | 'midnight' | 'carbon' | 'black' | 'forest' | 'plum';
  lightSurface: 'paper' | 'snow' | 'sand';
  backdrop: 'dots' | 'grid' | 'none';
};
export const APPEARANCE_KEY = 'proharness.appearance.v1';
export const DEFAULT_APPEARANCE: Appearance = { theme: 'dark', accent: 'mint', density: 'comfortable', motion: 'system', surface: 'graphite', lightSurface: 'paper', backdrop: 'dots' };
export const DARK_SURFACES = [
  {id:'carbon', label:'Carbon'}, {id:'black', label:'Black'}, {id:'graphite', label:'Charcoal'},
  {id:'midnight', label:'Navy'}, {id:'forest', label:'Forest'}, {id:'plum', label:'Plum'},
] as const;
export const LIGHT_SURFACES = [{id:'paper', label:'Paper'}, {id:'snow', label:'Snow'}, {id:'sand', label:'Sand'}] as const;
export const ACCENTS = [
  {id:'neutral',label:'Neutral',color:'#dedee2',lightColor:'#45454b'},
  {id:'mint',label:'Mint',color:'#8ee3c4',lightColor:'#20785b'},
  {id:'blue',label:'Blue',color:'#9bbcff',lightColor:'#426eb8'},
  {id:'violet',label:'Violet',color:'#c4afff',lightColor:'#7044b8'},
  {id:'cyan',label:'Cyan',color:'#83dce8',lightColor:'#176e7c'},
  {id:'amber',label:'Amber',color:'#ebc47d',lightColor:'#926520'},
  {id:'orange',label:'Orange',color:'#f2b087',lightColor:'#a34d20'},
  {id:'rose',label:'Rose',color:'#efabc5',lightColor:'#a34970'},
] as const;
const choices = { theme: ['dark', 'light', 'system'], accent: ACCENTS.map(item=>item.id) as string[], density: ['comfortable', 'compact'], motion: ['system', 'reduced'], surface: DARK_SURFACES.map(item=>item.id) as string[], lightSurface: LIGHT_SURFACES.map(item=>item.id) as string[], backdrop: ['dots', 'grid', 'none'] };
export function validateAppearance(value: unknown): Appearance {
  const result = { ...DEFAULT_APPEARANCE };
  if (!value || typeof value !== 'object') return result;
  for (const key of Object.keys(choices) as (keyof Appearance)[]) {
    const candidate = (value as Record<string, unknown>)[key];
    if (typeof candidate === 'string' && choices[key].includes(candidate)) Object.assign(result, { [key]: candidate });
  }
  return result;
}
export const APPEARANCE_PRESETS = [
  { id: 'carbon', name: 'Carbon', note: 'Deep neutral gray', theme: 'dark', accent: 'neutral', surface: 'carbon', lightSurface: 'paper', backdrop: 'none' },
  { id: 'black', name: 'Black', note: 'Near-black · minimal', theme: 'dark', accent: 'neutral', surface: 'black', lightSurface: 'paper', backdrop: 'none' },
  { id: 'graphite', name: 'Graphite', note: 'Soft charcoal · mint', theme: 'dark', accent: 'mint', surface: 'graphite', lightSurface: 'paper', backdrop: 'dots' },
  { id: 'midnight', name: 'Midnight', note: 'Deep navy · blue', theme: 'dark', accent: 'blue', surface: 'midnight', lightSurface: 'paper', backdrop: 'grid' },
  { id: 'forest', name: 'Forest', note: 'Evergreen · mint', theme: 'dark', accent: 'mint', surface: 'forest', lightSurface: 'paper', backdrop: 'dots' },
  { id: 'plum', name: 'Plum', note: 'Aubergine · violet', theme: 'dark', accent: 'violet', surface: 'plum', lightSurface: 'paper', backdrop: 'none' },
  { id: 'ember', name: 'Ember', note: 'Charcoal · amber', theme: 'dark', accent: 'amber', surface: 'graphite', lightSurface: 'paper', backdrop: 'none' },
  { id: 'copper', name: 'Copper', note: 'Carbon · orange', theme: 'dark', accent: 'orange', surface: 'carbon', lightSurface: 'paper', backdrop: 'dots' },
  { id: 'glacier', name: 'Glacier', note: 'Navy · cyan', theme: 'dark', accent: 'cyan', surface: 'midnight', lightSurface: 'paper', backdrop: 'grid' },
  { id: 'paper', name: 'Paper', note: 'Soft light · rose', theme: 'light', accent: 'rose', surface: 'graphite', lightSurface: 'paper', backdrop: 'dots' },
  { id: 'snow', name: 'Snow', note: 'Crisp white · neutral', theme: 'light', accent: 'neutral', surface: 'carbon', lightSurface: 'snow', backdrop: 'none' },
  { id: 'sand', name: 'Sand', note: 'Warm ivory · amber', theme: 'light', accent: 'amber', surface: 'graphite', lightSurface: 'sand', backdrop: 'dots' },
] as const satisfies readonly ({id:string;name:string;note:string} & Pick<Appearance,'theme'|'accent'|'surface'|'lightSurface'|'backdrop'>)[];
export function applyAppearancePreset(id: string) {
  const preset = APPEARANCE_PRESETS.find(item => item.id === id);
  if (preset) setAppearance({ theme: preset.theme, accent: preset.accent, backdrop: preset.backdrop,
    ...(preset.theme === 'dark' ? {surface:preset.surface} : {lightSurface:preset.lightSurface}) });
}
function read(): Appearance {
  try { return validateAppearance(JSON.parse(localStorage.getItem(APPEARANCE_KEY) || 'null')); } catch { return { ...DEFAULT_APPEARANCE }; }
}
let current = read();
const listeners = new Set<() => void>();
function apply() {
  const root = document.documentElement;
  root.dataset.theme = current.theme === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : current.theme;
  root.dataset.surface = current.surface;
  root.dataset.lightSurface = current.lightSurface;
  root.dataset.backdrop = current.backdrop;
  root.dataset.accent = current.accent;
  root.dataset.density = current.density;
  root.dataset.motion = current.motion === 'reduced' || matchMedia('(prefers-reduced-motion: reduce)').matches ? 'reduced' : 'system';
  const darkColors = {graphite:'#101315',midnight:'#0b101b',carbon:'#0c0c0d',black:'#050505',forest:'#0b1310',plum:'#120e16'};
  const lightColors = {paper:'#f3f6f5',snow:'#f7f7f8',sand:'#f6f1e7'};
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', root.dataset.theme === 'dark' ? darkColors[current.surface] : lightColors[current.lightSurface]);
}
function publish() { apply(); listeners.forEach(listener => listener()); }
export function setAppearance(patch: Partial<Appearance>) {
  current = validateAppearance({ ...current, ...patch });
  try { localStorage.setItem(APPEARANCE_KEY, JSON.stringify(current)); } catch { /* Preferences remain usable when storage is unavailable. */ }
  publish();
}
export function resetAppearance() { setAppearance(DEFAULT_APPEARANCE); }
apply();
for (const query of ['(prefers-color-scheme: dark)', '(prefers-reduced-motion: reduce)']) matchMedia(query).addEventListener('change', apply);
window.addEventListener('storage', event => { if (event.key === APPEARANCE_KEY || event.key === null) { current = read(); publish(); } });
export function useAppearance() {
  const appearance = useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => current);
  return { appearance, setAppearance, resetAppearance };
}
