import {useEffect,useRef,useState} from 'react';
import {errorMessage,isAbort,libraryApi,type LibraryDocument} from '../lib/libraryApi';
import {validateOriginalPage,originalPageReference,documentOriginalHash,type OriginalPageImage,type OriginalPageReference} from '../lib/pageImages';

interface Props {document:LibraryDocument;includePrivate:boolean;initialPage:number;pageKnown:boolean;active:boolean;expectedReference?:OriginalPageReference;onSelectOriginalPage?:(ref:OriginalPageReference)=>void;selectionDisabled?:boolean}
interface Result {scope:string;page:number;packet:OriginalPageImage|null;busy:boolean;error:string}
/** An explicit, ephemeral view of original PDF pixels; this never extracts or proposes text. */
export function OriginalPagePreview({document:doc,includePrivate,initialPage,pageKnown,active,expectedReference,onSelectOriginalPage,selectionDisabled=false}:Props){
 const [,setContext]=useState(0);
 useEffect(()=>libraryApi.subscribeInvalidation?.(()=>setContext(value=>value+1)),[]);
 const contextVersion=libraryApi.contextVersion;
 const scope=JSON.stringify([doc.document_id,doc.version,documentOriginalHash(doc),initialPage,includePrivate,contextVersion,expectedReference]);
 const identity=useRef(scope);identity.current=scope;
 const currentActive=useRef(active);currentActive.current=active;
 const sequence=useRef(0);
 const [chosen,setChosen]=useState({scope,page:initialPage}),[result,setResult]=useState<Result|null>(null),[retry,setRetry]=useState(0);
 const [displayed,setDisplayed]=useState<{scope:string;packet:OriginalPageImage}|null>(null);
 const [detail,setDetail]=useState({scope,zoom:100,expanded:false});
 const page=chosen.scope===scope?chosen.page:initialPage;
 const currentPage=useRef(page);currentPage.current=page;
 const currentResult=useRef(result);currentResult.current=result;
 const packet=result?.scope===scope?result.packet:null,visible=packet?.page===page?packet:null;
 const busy=active&&(!result||result.scope!==scope||result.page!==page||result.busy);
 const error=result?.scope===scope&&result.page===page?result.error:'';
 const zoom=detail.scope===scope?detail.zoom:100,expanded=detail.scope===scope&&detail.expanded;
 useEffect(()=>{
  const token=++sequence.current,controller=new AbortController();
  const current=()=>!controller.signal.aborted&&identity.current===scope&&currentPage.current===page&&currentActive.current&&libraryApi.contextVersion===contextVersion&&sequence.current===token;
  if(active&&!(visible&&!error)){
   setResult({scope,page,packet,busy:true,error:''});
   libraryApi.request<unknown>(`documents/${encodeURIComponent(doc.document_id)}/pages/${page}/image`,{include_sensitive:includePrivate,max_side:2048},undefined,undefined,controller.signal)
    .then(value=>{if(!current())return;const valid=validateOriginalPage(value,doc,page);const image=valid&&(!expectedReference||(valid.document_id===expectedReference.document_id&&valid.version===expectedReference.version&&valid.original_content_hash===expectedReference.original_content_hash))?valid:null;setResult({scope,page,packet:image,busy:false,error:image?'':'The page response did not match this source version or its image limits. The saved original page is unavailable; it has not been replaced with another source.'});})
    .catch(error=>{if(current())setResult({scope,page,packet:null,busy:false,error:isAbort(error)?'':errorMessage(error)});});
  }else if(active&&visible&&(result?.busy||result?.page!==page))setResult({scope,page,packet:visible,busy:false,error:''});
  return()=>{sequence.current++;controller.abort();};
 },[scope,active,page,retry]);
 function navigate(next:number){if(next<1||!packet||next>packet.page_count)return;setChosen({scope,page:next});}
 function setZoom(next:number){setDetail({scope,zoom:Math.max(50,Math.min(300,next)),expanded});}
 return <section hidden={!active} className={`detail-section original-page-preview${expanded?' expanded':''}`} aria-label="Original PDF page" aria-busy={busy}>
  <div className="page-preview-heading"><h3>Original page</h3><span>Version {doc.version} · PDF</span></div>
  <p className="field-note">Original file appearance. Extracted text may differ; viewing a page does not verify its claims.{!pageKnown?' No cited page is known; starting at page 1.':''}</p>
  <div className="page-preview-controls" role="group" aria-label="Page navigation">
   <div className="page-preview-pagination">
   <button className="button small" disabled={page<=1||!packet} onClick={()=>navigate(page-1)} aria-label="Previous original page">←</button>
   <span role="status" aria-live="polite" aria-atomic="true">Page {page}{packet?` of ${packet.page_count}`:''}</span>
   <button className="button small" disabled={!packet||page>=packet.page_count} onClick={()=>navigate(page+1)} aria-label="Next original page">→</button>
   </div>
   <div className="page-preview-zoom" role="group" aria-label="Image detail">
    <button className="button small" disabled={!visible||zoom<=50} onClick={()=>setZoom(zoom-25)} aria-label="Zoom out">−</button>
    <button className="text-button" disabled={!visible} onClick={()=>setZoom(100)} aria-label="Fit page width">{zoom}%</button>
    <button className="button small" disabled={!visible||zoom>=300} onClick={()=>setZoom(zoom+25)} aria-label="Zoom in">+</button>
    <button className="button small" disabled={!visible} aria-pressed={expanded} onClick={()=>setDetail({scope,zoom,expanded:!expanded})}>{expanded?'Compact':'Expand'}</button>
   </div>
  </div>
  {onSelectOriginalPage&&<button className="button small" disabled={!active||busy||!!error||!visible||displayed?.scope!==scope||displayed.packet!==visible||selectionDisabled} onClick={()=>{
   if(!currentActive.current||identity.current!==scope||libraryApi.contextVersion!==contextVersion||currentPage.current!==page||currentResult.current?.packet!==visible||!visible||displayed?.scope!==scope||displayed.packet!==visible||selectionDisabled)return;
   onSelectOriginalPage(originalPageReference(visible.source_reference));
  }}>Select page for collection</button>}
  {busy&&<div className="page-preview-placeholder" role="status">Loading original page {page}…</div>}
  {error&&<div className="page-preview-error"><p role="alert">{error}</p><button className="button small" onClick={()=>setRetry(value=>value+1)}>Retry page</button></div>}
  {visible&&!busy&&!error&&<>
   <div className="page-preview-viewport" role="region" aria-label={`Original page ${page}, version ${doc.version}. Arrow keys change pages; plus and minus zoom.`} tabIndex={0} onKeyDown={event=>{
    if(event.target!==event.currentTarget||event.altKey||event.ctrlKey||event.metaKey)return;
    if(event.key==='ArrowLeft'){event.preventDefault();navigate(page-1);}else if(event.key==='ArrowRight'){event.preventDefault();navigate(page+1);}
    else if(event.key==='+'||event.key==='='){event.preventDefault();setZoom(zoom+25);}else if(event.key==='-'){event.preventDefault();setZoom(zoom-25);}else if(event.key==='Home'){event.preventDefault();setZoom(100);}
   }}>
    <img src={`data:image/png;base64,${visible.image.data}`} alt={`Original PDF page ${page} of ${visible.page_count} from ${doc.title}, version ${doc.version}`} width={visible.width} height={visible.height} style={{width:`${zoom}%`}} onLoad={()=>{if(identity.current===scope&&currentPage.current===page&&currentActive.current&&libraryApi.contextVersion===contextVersion&&currentResult.current?.packet===visible)setDisplayed({scope,packet:visible});}} onError={()=>{
     if(identity.current===scope&&currentPage.current===page&&currentActive.current&&currentResult.current?.packet===visible)setResult({scope,page,packet:null,busy:false,error:'This page image could not be displayed. Retry or download the original.'});
    }}/>
   </div>
   <p className="field-note page-preview-caption">{visible.width} × {visible.height} pixels · Zoom changes display size. Use the source text view for selectable wording.</p>
  </>}
 </section>;
}
