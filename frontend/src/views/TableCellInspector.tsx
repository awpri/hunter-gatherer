import {useEffect,useRef,useState} from 'react';
import {errorMessage,isAbort,libraryApi,type LibraryDocument} from '../lib/libraryApi';
import {originalSourceHref,tableOmissionMessage,tableStructure,type TableWindow} from '../lib/tableCells';

interface Props {document:LibraryDocument;chunkId:string;includePrivate:boolean;onOriginalPage?:(page?:number)=>void}
interface Page {scope:string;offset:number;previous:number[];budget:number;packet:TableWindow}
/** Explicit, read-only view of saved cells; exact source wording stays in SourceReader. */
export function TableCellInspector({document:doc,chunkId,includePrivate,onOriginalPage}:Props){
 const scope=JSON.stringify([doc.document_id,doc.version,chunkId,includePrivate]);
 const identity=useRef(scope);identity.current=scope;
 const pending=useRef<AbortController|null>(null),sequence=useRef(0);
 const [page,setPage]=useState<Page|null>(null),[status,setStatus]=useState<{scope:string;busy:boolean;error:string}|null>(null);
 useEffect(()=>()=>{sequence.current++;pending.current?.abort();},[scope]);
 const visible=page?.scope===scope?page:null,busy=status?.scope===scope&&status.busy,error=status?.scope===scope?status.error:'';
 const structure=visible?tableStructure(visible.packet,doc.document_id,chunkId,doc.version,visible.offset):null;
 async function load(offset=0,previous:number[]=[],budget=4096){
  pending.current?.abort();const controller=new AbortController();pending.current=controller;const request=++sequence.current;
  setStatus({scope,busy:true,error:''});
  const current=()=>!controller.signal.aborted&&identity.current===scope&&sequence.current===request;
  try {
   const packet=await libraryApi.request<TableWindow>(`chunks/${encodeURIComponent(chunkId)}/window`,{include_sensitive:includePrivate,before:0,after:0,max_tokens:budget,table_cell_offset:offset,table_cell_limit:32},undefined,undefined,controller.signal);
   if(current()){setPage({scope,offset,previous,budget,packet});setStatus({scope,busy:false,error:''});}
  }catch(error){if(current())setStatus({scope,busy:false,error:isAbort(error)?'':errorMessage(error)});}
 }
 const anchor=visible?.packet.chunks?.find(chunk=>chunk.chunk_id===chunkId&&chunk.document_id===doc.document_id&&chunk.version===doc.version);
 const anchorPage=Number.isSafeInteger(anchor?.location?.page)&&Number(anchor?.location?.page)>0?Number(anchor?.location?.page):undefined;
 const hasNext=structure?.next_cell_offset!=null&&structure.next_cell_offset>visible!.offset;
 return <section className="detail-section table-inspector" aria-label="Saved table cell inspector" aria-busy={!!busy}>
  <div className="action-row"><h3>Table cells</h3><button className="button small" disabled={!!busy} onClick={()=>load()}>Inspect table</button></div>
  {busy&&<p role="status">Reading saved table cells…</p>}{error&&<p role="alert">{error}</p>}
  {visible&&<>
   <p className="field-note">Model predictions from version {doc.version}. Cell text, spans and header flags can be wrong. The anchor identifies the extraction passage; it is not a cell quotation or cell citation.</p>
   {onOriginalPage?<button className="text-button" disabled={!!busy} onClick={()=>onOriginalPage(anchorPage)}>Original page{anchorPage?` · Page ${anchorPage}`:''}</button>:<a className="text-button" href={originalSourceHref(doc,includePrivate)} target="_blank" rel="noreferrer">Download original</a>}
   {!structure&&<p role="status">{visible.packet.table_structure!==undefined?'Saved cell response is inconsistent or malformed; no cells are displayed.':tableOmissionMessage(visible.packet.table_structure_omitted?.reason)}</p>}
   {structure&&<>
    <p className="field-note">{structure.total_cell_count} raw cells · Predicted shape {structure.num_rows??'unknown'} × {structure.num_cols??'unknown'} · {structure.table_ref} · {structure.orientation??'unknown orientation'}</p>
    <p className="field-note">Original list order. Row and column ranges are zero-based, end-exclusive. Overlaps and invalid spans are kept as recorded; no grid is reconstructed.</p>
    {structure.status==='omitted'?<p role="status">Saved cell metadata was omitted: {structure.omission_reason?.replaceAll('_',' ')}.</p>:<>
     <p className="field-note">{structure.observations.length?`Recorded shape observations: ${structure.observations.map(item=>item.replaceAll('_',' ')).join('; ')}.`:'No shape observations were recorded. This does not establish correct extraction.'}</p>
     {structure.total_cell_count===0&&<p role="status">The saved model snapshot contains no cells.</p>}
     <ol className="table-cell-list" aria-label="Raw model cells">{structure.cells.map(cell=><li key={cell.ordinal}>
      <div className="table-cell-coordinates">Cell {cell.ordinal} · Rows [{cell.start_row_offset_idx}, {cell.end_row_offset_idx}) · Columns [{cell.start_col_offset_idx}, {cell.end_col_offset_idx}) · Spans {cell.row_span} × {cell.col_span}</div>
      <pre className="table-cell-text">{cell.text}</pre>{cell.text===''&&<p className="field-note">Empty model text.</p>}
      <p className="field-note">Predicted flags: column header {String(cell.column_header)} · row header {String(cell.row_header)} · row section {String(cell.row_section)} · fillable {String(cell.fillable)}</p>
      {cell.ref!==null&&<p className="field-note">Unresolved model reference: <code>{cell.ref}</code></p>}
      {cell.bbox&&<details><summary>Raw bounding box · no cell page association</summary><pre>{JSON.stringify(cell.bbox,null,2)}</pre></details>}
     </li>)}</ol>
     {structure.omitted_cells.map(cell=><p className="field-note" key={cell.ordinal}>Cell {cell.ordinal} omitted: {cell.reason.replaceAll('_',' ')}. Its text is not shortened or reconstructed.</p>)}
     {structure.budget_blocked&&<p role="status">The read budget blocked remaining cells at offset {structure.next_cell_offset}. A larger budget may help.</p>}
    </>}
   </>}
   <div className="action-row">
    <button className="button small" disabled={!!busy||visible.offset===0} onClick={()=>load(0,[],visible.budget)}>First cells</button>
    <button className="button small" disabled={!!busy||!visible.previous.length} onClick={()=>load(visible.previous.at(-1)!,visible.previous.slice(0,-1),visible.budget)}>Previous cells</button>
    <button className="button small" disabled={!!busy||!hasNext} onClick={()=>load(structure!.next_cell_offset!,[...visible.previous,visible.offset],visible.budget)}>Next cells</button>
    {(!structure||structure.budget_blocked)&&visible.budget<8192&&<button className="button small" disabled={!!busy} onClick={()=>load(visible.offset,visible.previous,8192)}>Retry with larger budget</button>}
   </div>
  </>}
 </section>;
}
