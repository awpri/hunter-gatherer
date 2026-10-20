import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { ACCENTS, DARK_SURFACES, LIGHT_SURFACES, APPEARANCE_PRESETS, applyAppearancePreset, useAppearance } from '../lib/appearance';
import './AppearancePanel.css';
import './ThemePresets.css';

type Props = { open: boolean; onClose: () => void };
export function AppearancePanel({ open, onClose }: Props) {
  const { appearance, setAppearance, resetAppearance } = useAppearance();
  const [section,setSection] = useState<'themes'|'customize'>('themes');
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.querySelector<HTMLElement>('button')?.focus();
    function handleKey(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close.current(); }
      if (event.key === 'Tab') {
        const nodes = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),[tabindex="0"]') || []);
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }
    document.addEventListener('keydown', handleKey, true);
    return () => { document.removeEventListener('keydown', handleKey, true); document.body.style.overflow = previousOverflow; previous?.focus(); };
  }, [open]);
  if (!open) return null;
  return <div className="modal-backdrop appearance-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={panel} className="modal appearance-panel" role="dialog" aria-modal="true" aria-labelledby="appearance-title">
      <header><div><h2 id="appearance-title">Appearance</h2></div><button className="icon-button" aria-label="Close appearance" onClick={onClose}>×</button></header>
      <div className="appearance-sections" role="tablist" aria-label="Appearance options">
        {(['themes','customize'] as const).map((item,index)=><button key={item} id={`appearance-tab-${item}`} role="tab" aria-selected={section===item} aria-controls={`appearance-${item}`} tabIndex={section===item?0:-1}
          onClick={()=>setSection(item)} onKeyDown={event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?'themes':event.key==='End'?'customize':index===0?'customize':'themes';setSection(next);panel.current?.querySelector<HTMLButtonElement>(`#appearance-tab-${next}`)?.focus();}}}>{item==='themes'?'Themes':'Customize'}</button>)}
      </div>
      <div className="appearance-body">
        {section==='themes'?<div id="appearance-themes" role="tabpanel" aria-labelledby="appearance-tab-themes" className="appearance-theme-groups">
          {(['dark','light'] as const).map(theme=><fieldset key={theme}><legend>{theme==='dark'?'Dark':'Light'}</legend><div className="appearance-presets">{APPEARANCE_PRESETS.filter(preset=>preset.theme===theme).map(preset=>{
            const accent=ACCENTS.find(item=>item.id===preset.accent)!;
            return <button key={preset.id} className="appearance-preset" aria-pressed={appearance.theme===preset.theme && appearance.accent===preset.accent && (theme==='dark'?appearance.surface===preset.surface:appearance.lightSurface===preset.lightSurface) && appearance.backdrop===preset.backdrop} onClick={()=>applyAppearancePreset(preset.id)}>
              <span className="preset-preview appearance-theme-sample" data-theme={theme} data-surface={preset.surface} data-light-surface={preset.lightSurface} style={{'--sample-accent':theme==='dark'?accent.color:accent.lightColor} as CSSProperties} aria-hidden="true"><i/><b/><em/><span/></span>
              <strong>{preset.name}</strong><small>{preset.note}</small>
            </button>;
          })}</div></fieldset>)}
        </div>:<div id="appearance-customize" role="tabpanel" aria-labelledby="appearance-tab-customize" className="appearance-customize">
        <fieldset><legend>Theme</legend><div className="appearance-options">{(['dark','light','system'] as const).map(theme => <button key={theme} type="button" aria-pressed={appearance.theme === theme} onClick={() => setAppearance({theme})}><span aria-hidden="true">{theme === 'dark' ? '◐' : theme === 'light' ? '☼' : '◒'}</span>{theme[0].toUpperCase()+theme.slice(1)}</button>)}</div></fieldset>
        {appearance.theme!=='light'&&<fieldset><legend>Dark surface</legend><div className="appearance-surface-options">{DARK_SURFACES.map(surface=><button key={surface.id} aria-pressed={appearance.surface===surface.id} onClick={()=>setAppearance({surface:surface.id})}><span className="appearance-theme-sample surface-preview" data-theme="dark" data-surface={surface.id} aria-hidden="true"><i/></span>{surface.label}</button>)}</div></fieldset>}
        {appearance.theme!=='dark'&&<fieldset><legend>Light surface</legend><div className="appearance-surface-options">{LIGHT_SURFACES.map(surface=><button key={surface.id} aria-pressed={appearance.lightSurface===surface.id} onClick={()=>setAppearance({lightSurface:surface.id})}><span className="appearance-theme-sample surface-preview" data-theme="light" data-light-surface={surface.id} aria-hidden="true"><i/></span>{surface.label}</button>)}</div></fieldset>}
        <fieldset><legend>Accent</legend><div className="appearance-swatches">{ACCENTS.map(accent=><button key={accent.id} className="appearance-swatch" style={{'--swatch':accent.color} as CSSProperties} aria-label={`${accent.label} accent`} aria-pressed={appearance.accent===accent.id} onClick={()=>setAppearance({accent:accent.id})}><span aria-hidden="true">{appearance.accent===accent.id?'✓':''}</span><small>{accent.label}</small></button>)}</div></fieldset>
        <fieldset><legend>Map backdrop</legend><div className="appearance-options">{(['dots','grid','none'] as const).map(backdrop => <button key={backdrop} aria-pressed={appearance.backdrop === backdrop} onClick={() => setAppearance({backdrop})}>{backdrop === 'none' ? 'Clear' : backdrop[0].toUpperCase()+backdrop.slice(1)}</button>)}</div></fieldset>
        <fieldset><legend>Density</legend><div className="appearance-options">{(['comfortable','compact'] as const).map(density => <button key={density} aria-pressed={appearance.density === density} onClick={() => setAppearance({density})}>{density[0].toUpperCase()+density.slice(1)}</button>)}</div></fieldset>
        <fieldset><legend>Motion</legend><div className="appearance-options">{(['system','reduced'] as const).map(motion => <button key={motion} aria-pressed={appearance.motion === motion} onClick={() => setAppearance({motion})}>{motion === 'system' ? 'Subtle' : 'Reduced'}</button>)}</div><p className="appearance-note">Both modes respond immediately. Subtle adds button feedback. Your device’s reduced motion setting is always respected.</p></fieldset>
        </div>}
      </div>
      <footer><button className="text-button" onClick={resetAppearance}>Reset</button><span className="appearance-saved">Saved on this device</span><button className="button primary" onClick={onClose}>Done</button></footer>
    </div>
  </div>;
}

export default AppearancePanel;
