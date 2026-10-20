import type {LibraryDocument} from '../lib/libraryApi';
import {mapSourceText} from '../lib/sourceCitations';
import {fileType} from '../lib/fileTypes';
import type {Selection} from './LibraryInspector';
import './SourceText.css';

/** Shared by both document entry points so section text gets the same citation rules. */
export function SourceText({doc, select, disabled = false}: {doc: LibraryDocument; select: (selection: Selection) => void; disabled?: boolean}) {
  const mapped = mapSourceText(doc);
  const pageName = fileType(doc).id === 'slides' ? 'Slide' : 'Page';
  return <>
    {!!doc.passages?.length && <p className="field-note source-mapping-note">{
      mapped.status === 'missing' ? 'Passage citations are unavailable for this source text.' :
      mapped.status === 'invalid' ? 'Passage citations could not be matched safely to this source text.' :
      mapped.coverage === 'none' ? 'This source text has no mapped passage citations.' :
      mapped.coverage === 'partial' ? 'Some source text has no mapped citation. Uncited text is marked below.' :
      'Open a passage citation to read its exact source context.'
    }</p>}
    {mapped.status === 'mapped' && !mapped.complete && <p className="field-note source-mapping-note" role="status">Passage citation mapping reached its limit. Some available citations may be omitted.</p>}
    {doc.passages?.map((passage, index) => <article key={`${passage.section_id}-${index}`}>
      <span className="passage-location">{passage.section || (passage.page ? `${pageName} ${passage.page}` : 'Section')}</span>
      <div className="record-body mapped-source-text">{mapped.passages[index].map((segment, part) => <span className={`source-text-span ${segment.citation ? 'cited' : 'uncited'}`} key={part}>
        <span data-source-text="true">{segment.text}</span>
        {segment.citation ? <button className="text-button source-span-citation" disabled={disabled}
          title={`Open exact passage ${segment.citation}`} aria-label={`Open exact passage ${segment.citation}`}
          onClick={() => select({type: 'document', id: doc.document_id, chunk: segment.chunk_id})}>
          <code>{segment.citation}</code><span aria-hidden="true"> ↗</span>
        </button> : mapped.status === 'mapped' && segment.text.length > 0 ? <span className="source-span-gap">Uncited text</span> : null}
      </span>)}</div>
    </article>)}
    {!doc.passages?.length && <p className="muted">No extractable text in this selection.</p>}
  </>;
}
