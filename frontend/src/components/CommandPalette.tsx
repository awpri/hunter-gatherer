import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import './CommandPalette.css';

export interface CommandPaletteCommand {
  id: string;
  label: string;
  description?: string;
  section?: string;
  icon?: ReactNode;
  keywords?: string[];
  shortcut?: string;
  onSelect: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  commands: CommandPaletteCommand[];
  onSearch: (query: string) => void;
}

function SearchGlyph() {
  return <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5" /><path d="m13 13 4 4" /></svg>;
}

export default function CommandPalette({ open, onClose, commands, onSearch }: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const id = useId();
  const trimmed = query.trim();
  const matches = useMemo(() => {
    const terms = trimmed.toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return commands.filter(command => {
      const haystack = [command.label, command.description, command.section, ...(command.keywords ?? [])].join(' ').toLocaleLowerCase();
      return terms.every(term => haystack.includes(term));
    });
  }, [commands, trimmed]);
  const shown = matches.slice(0, 12);
  const count = shown.length + (trimmed ? 1 : 0);
  const selected = Math.min(active, Math.max(0, count - 1));

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    setQuery('');
    setActive(0);
    input.current?.focus();
    return () => {
      document.body.style.overflow = oldOverflow;
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);

  useEffect(() => {
    if (open) document.getElementById(`${id}-option-${selected}`)?.scrollIntoView({ block: 'nearest' });
  }, [id, selected, open]);

  function choose(index: number) {
    const command = shown[index];
    onClose();
    if (command) command.onSelect();
    else if (trimmed) onSearch(trimmed);
  }

  if (!open) return null;

  return createPortal(
    <div className="command-palette-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialog} className="command-palette" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose(); }
          else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (count) setActive((selected + (event.key === 'ArrowDown' ? 1 : -1) + count) % count);
          } else if (event.key === 'Enter' && event.target === input.current && count) {
            event.preventDefault(); choose(selected);
          } else if (event.key === 'Tab') {
            const focusable = dialog.current?.querySelectorAll<HTMLElement>('input, button');
            if (!focusable?.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
          }
        }}>
        <h2 id={`${id}-title`} className="command-palette-sr">Quick actions and library search</h2>
        <div className="command-palette-input-row">
          <SearchGlyph />
          <input ref={input} value={query} onChange={event => { setQuery(event.target.value); setActive(0); }}
            placeholder="Find an action or search your library…" aria-label="Find an action or search your library"
            role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls={`${id}-list`}
            aria-activedescendant={count ? `${id}-option-${selected}` : undefined} autoComplete="off" spellCheck={false} />
          <button className="command-palette-close" onClick={onClose} aria-label="Close command palette">esc</button>
        </div>
        <div className="command-palette-results" id={`${id}-list`} role="listbox" aria-label="Actions">
          {shown.map((command, index) => <div key={command.id}>
            {(index === 0 || command.section !== shown[index - 1].section) && <div className="command-palette-section" aria-hidden="true">{command.section ?? 'Quick actions'}</div>}
            <div id={`${id}-option-${index}`} role="option" aria-selected={selected === index}
              className={`command-palette-option${selected === index ? ' is-active' : ''}`}
              onMouseEnter={() => setActive(index)} onMouseDown={event => event.preventDefault()} onClick={() => choose(index)}>
              <span className="command-palette-icon" aria-hidden="true">{command.icon ?? <span>↗</span>}</span>
              <span className="command-palette-copy"><span className="command-palette-label">{command.label}</span>{command.description && <span className="command-palette-description">{command.description}</span>}</span>
              {command.shortcut && <kbd>{command.shortcut}</kbd>}
              {selected === index && <span className="command-palette-return" aria-hidden="true">↵</span>}
            </div>
          </div>)}
          {trimmed && <>
            <div className="command-palette-section" aria-hidden="true">Library search</div>
            <div id={`${id}-option-${shown.length}`} role="option" aria-selected={selected === shown.length}
              className={`command-palette-option${selected === shown.length ? ' is-active' : ''}`}
              onMouseEnter={() => setActive(shown.length)} onMouseDown={event => event.preventDefault()} onClick={() => choose(shown.length)}>
              <span className="command-palette-icon"><SearchGlyph /></span>
              <span className="command-palette-copy"><span className="command-palette-label">Search library for “{trimmed}”</span><span className="command-palette-description">Find documents, notes, and saved knowledge</span></span>
              {selected === shown.length && <span className="command-palette-return" aria-hidden="true">↵</span>}
            </div>
          </>}
          {!count && <div className="command-palette-empty">Type to search your library.</div>}
        </div>
        <div className="command-palette-footer"><span><kbd>↑</kbd><kbd>↓</kbd> move <kbd>↵</kbd> select</span><span>{matches.length > 12 ? `${matches.length - 12} more · keep typing` : 'esc to close'}</span></div>
        <span className="command-palette-sr" role="status" aria-live="polite">{count} options available</span>
      </div>
    </div>, document.body,
  );
}
