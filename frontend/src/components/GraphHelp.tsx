import {useState} from 'react';
import {createPortal} from 'react-dom';
import {Icon, Modal} from '../views/LibraryShared';
import './GraphHelp.css';

export function GraphHelp({constellation, truncated, omitted, warnings}: {
  constellation: boolean; truncated: boolean; omitted: number; warnings: string[];
}) {
  const [open, setOpen] = useState(false);
  return <>
    <button className="cm-help" type="button" aria-label="Map information" title="Map controls & connections"
      aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}><Icon name="info" size={16}/></button>
    {open && createPortal(<Modal title="Map controls" close={() => setOpen(false)}>
      <div className="graph-help-content">
        <dl className="graph-help-shortcuts">
          <div><dt>Zoom</dt><dd>Scroll or pinch</dd></div>
          <div><dt>Pan</dt><dd><kbd>Shift</kbd> + scroll, or drag</dd></div>
          <div><dt>Explore</dt><dd>Select a group, then a record</dd></div>
          <div><dt>Keyboard</dt><dd><kbd>Tab</kbd> enters groups and records. Arrow keys move within a group; <kbd>Enter</kbd> opens a record.</dd></div>
          <div><dt>Reset view</dt><dd>Fit all, or <kbd>Esc</kbd> on the board</dd></div>
        </dl>
        <h3>Connections</h3>
        <p>Hover a file to highlight its citations. The board stays still until you navigate.</p>
        <p>Solid lines are recorded citations. Dashed lines are saved topic paths, shown after selection. Neither verifies a claim.</p>
        <details className="graph-help-notes"><summary tabIndex={0}>About this view</summary>
          <p>{constellation ? 'Constellation places connected groups near one another.' : 'Board keeps groups in packed panels.'} Placement never implies evidence.</p>
          {truncated && <p>Search or choose a group to see records beyond this view.</p>}
          {omitted > 0 && <p>{omitted} additional records are available in the list.</p>}
          {warnings.map((warning, index) => <p key={index}>{warning}</p>)}
        </details>
      </div>
    </Modal>, document.body)}
  </>;
}
