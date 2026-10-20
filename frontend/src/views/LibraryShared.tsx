import { useEffect, useId, useLayoutEffect, useRef, type ReactNode } from 'react';
export function Icon({name, size = 18}: {name: string; size?: number}) {
  // Selected Lucide SVG paths: ISC/MIT license in public/licenses/Lucide-LICENSE.txt.
  const paths: Record<string, ReactNode> = {
    topics: <><path d="m9 3-2 18M17 3l-2 18M4 8h17M3 16h17"/></>,
    source: <><path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z" />
  <path d="M14 2v5a1 1 0 0 0 1 1h5" />
  <path d="M10 9H8" />
  <path d="M16 13H8" />
  <path d="M16 17H8" /></>,
    workflow: <><rect width="8" height="8" x="3" y="3" rx="2" />
  <path d="M7 11v4a2 2 0 0 0 2 2h4" />
  <rect width="8" height="8" x="13" y="13" rx="2" /></>,
    preferences: <><path d="M10 5H3" />
  <path d="M12 19H3" />
  <path d="M14 3v4" />
  <path d="M16 17v4" />
  <path d="M21 12h-9" />
  <path d="M21 19h-5" />
  <path d="M21 5h-7" />
  <path d="M8 10v4" />
  <path d="M8 12H3" /></>,
    decision: <><circle cx="12" cy="18" r="3" />
  <circle cx="6" cy="6" r="3" />
  <circle cx="18" cy="6" r="3" />
  <path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9" />
  <path d="M12 12v3" /></>,
    project: <><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z" />
  <path d="M8 10v4" />
  <path d="M12 10v2" />
  <path d="M16 10v6" /></>,
    compass: <><circle cx="12" cy="12" r="10" />
  <path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z" /></>,
    education: <><path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 1.832l8.57 3.908a2 2 0 0 0 1.66 0z" />
  <path d="M22 10v6" />
  <path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5" /></>,
    archive: <><rect width="20" height="5" x="2" y="3" rx="1" />
  <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
  <path d="M10 12h4" /></>,
    knowledge: <><path d="M12 5v16" />
  <path d="M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z" /></>,
    work: <><path d="M12 12h.01" />
  <path d="M16 6V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2" />
  <path d="M22 13a18.15 18.15 0 0 1-20 0" />
  <rect width="20" height="14" x="2" y="6" rx="2" /></>,
    personal: <><circle cx="12" cy="8" r="5" />
  <path d="M20 21a8 8 0 0 0-16 0" /></>,
    grid: <><rect width="7" height="7" x="3" y="3" rx="1" />
  <rect width="7" height="7" x="14" y="3" rx="1" />
  <rect width="7" height="7" x="14" y="14" rx="1" />
  <rect width="7" height="7" x="3" y="14" rx="1" /></>,
    list: <><path d="M3 5h.01" />
  <path d="M3 12h.01" />
  <path d="M3 19h.01" />
  <path d="M8 5h13" />
  <path d="M8 12h13" />
  <path d="M8 19h13" /></>,
    memory: <><path d="M2 6h4" />
  <path d="M2 10h4" />
  <path d="M2 14h4" />
  <path d="M2 18h4" />
  <rect width="16" height="20" x="4" y="2" rx="2" />
  <path d="M9.5 8h5" />
  <path d="M9.5 12H16" />
  <path d="M9.5 16H14" /></>,
    lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/></>,
    unlock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0M12 14v3"/></>,
    upload: <><path d="M12 16V3m-4 4 4-4 4 4M5 15v5h14v-5"/></>,
    chevron: <path d="m9 6 6 6-6 6"/>,
    library: <><rect width="8" height="18" x="3" y="3" rx="1" />
  <path d="M7 3v18" />
  <path d="M20.4 18.9c.2.5-.1 1.1-.6 1.3l-1.9.7c-.5.2-1.1-.1-1.3-.6L11.1 5.1c-.2-.5.1-1.1.6-1.3l1.9-.7c.5-.2 1.1.1 1.3.6Z" /></>,
    connections: <><circle cx="5" cy="12" r="3"/><circle cx="18" cy="5" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8 10 7-4M8 14l7 4"/></>,
    review: <><rect width="8" height="4" x="8" y="2" rx="1" ry="1" />
  <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
  <path d="m9 14 2 2 4-4" /></>,
    health: <><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2" /></>,
    settings: <><path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z" />
  <path d="m2 22 3-3" />
  <path d="M7.5 13.5 10 11" />
  <path d="M10.5 16.5 13 14" />
  <path d="m18 3-4 4h6l-4 4" /></>,
    search: <><circle cx="10" cy="10" r="6.5"/><path d="m15 15 5 5"/></>,
    minus: <path d="M5 12h14"/>,
    pause: <><path d="M9 5v14M15 5v14" /></>,
    play: <path d="m8 5 11 7-11 7z" />,
    fit: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" />,
    overview: <><path d="M9 5 2 12l7 7M3 12h12a6 6 0 0 0 6-6" /></>,
    info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.01"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
    file: <><path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8M8 17h6"/></>,
    arrow: <path d="M5 12h14m-6-6 6 6-6 6"/>,
    close: <path d="m6 6 12 12M6 18 18 6"/>,
    pin: <><path d="m9 3 7 2-1 6 3 4-6 1-4 5 1-6-3-4 4-1z"/></>,
    moon: <path d="M20.8 13.1A9 9 0 0 1 10.9 3.2 9 9 0 1 0 20.8 13.1Z"/>,
    sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></>,
    sliders: <><path d="M4 6h5m4 0h7M4 12h10m4 0h2M4 18h2m4 0h10"/><circle cx="11" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="8" cy="18" r="2"/></>,
    command: <><path d="M8 8H5.5A2.5 2.5 0 1 1 8 5.5V8Zm8 0V5.5A2.5 2.5 0 1 1 18.5 8H16Zm0 8h2.5a2.5 2.5 0 1 1-2.5 2.5V16Zm-8 0v2.5A2.5 2.5 0 1 1 5.5 16H8Z"/><rect x="8" y="8" width="8" height="8"/></>,
    refresh: <><path d="M20 8a8 8 0 1 0 .4 7M20 3v5h-5"/></>,
  };
  return <svg className="library-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.file}</svg>;
}
export function EmptyState({title, children, action}: {title: string; children: ReactNode; action?: ReactNode}) {
  return <div className="empty-state"><span className="empty-line"/><h3>{title}</h3><p>{children}</p>{action}</div>;
}
export function ErrorNotice({message, retry}: {message: string; retry?: () => void}) {
  return <div className="error-notice" role="alert"><strong>Could not load.</strong><p>{message}</p>{retry && <button className="button small" onClick={retry}>Try again</button>}</div>;
}
export function Modal({title, children, close, busy = false}: {title: string; children: ReactNode; close: () => void; busy?: boolean}) {
  const ref = useRef<HTMLDivElement>(null); const titleId = useId();
  const closeRef = useRef(close); closeRef.current = close;
  const busyRef = useRef(busy); busyRef.current = busy;
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current!;
    const controls = () => Array.from(dialog.querySelectorAll<HTMLElement>('button, input, textarea, select, a[href], [tabindex="0"]')).filter(element => !element.matches(':disabled') && !element.closest('[hidden], [aria-hidden="true"]'));
    (controls()[0] || dialog).focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); if (!busyRef.current) closeRef.current();}
      if (event.key === 'Tab') { const all = controls(); const first = all[0], last = all[all.length-1];
        if (!first) {event.preventDefault(); dialog.focus();}
        else if (!dialog.contains(document.activeElement) || document.activeElement === dialog) {event.preventDefault(); (event.shiftKey ? last : first)?.focus();}
        else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', key); const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', key); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {if (busy && ref.current && (!ref.current.contains(document.activeElement) || document.activeElement?.matches(':disabled'))) ref.current.focus();}, [busy]);
  return <div className="modal-backdrop" onMouseDown={e => {if (e.target === e.currentTarget && !busy) close();}}><div ref={ref} className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-busy={busy} tabIndex={-1}><header><h2 id={titleId}>{title}</h2><button className="icon-button" aria-label="Close dialog" disabled={busy} onClick={close}><Icon name="close"/></button></header>{children}</div></div>;
}
