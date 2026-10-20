import {useEffect,useRef,useState} from 'react';
import {libraryApi,errorMessage,isAbort} from '../lib/libraryApi';
import {originalPageReference} from '../lib/pageImages';
import {validateRegionImage,type VisualRegion,type RegionImage} from '../lib/visualRegions';
import type {Selection} from './LibraryInspector';
import {Icon} from './LibraryShared';

export function VisualRegionReader({region,includePrivate,select,close,back}:{region:VisualRegion;includePrivate:boolean;select:(selection:Selection)=>void;close:()=>void;back?:()=>void}){
 const [view,setView]=useState<'region'|'page'>('region'),[retry,setRetry]=useState(0),[context,setContext]=useState(0);
 const [result,setResult]=useState<{scope:string;image?:RegionImage;error?:string}|null>(null);
 const panel=useRef<HTMLElement|null>(null);
 const scope=JSON.stringify([region,includePrivate,view,context,libraryApi.contextVersion]);
 const current=useRef(scope);current.current=scope;
 useEffect(()=>{panel.current?.focus();},[region.citation]);
 useEffect(()=>libraryApi.subscribeInvalidation?.(()=>setContext(value=>value+1)),[]);
 useEffect(()=>{
  const controller=new AbortController();
  libraryApi.request<unknown>(`regions/${encodeURIComponent(region.record_id)}/image`,{include_sensitive:includePrivate,view},undefined,undefined,controller.signal)
   .then(value=>{if(controller.signal.aborted||current.current!==scope)return;const image=validateRegionImage(value,region,view);setResult({scope,...(image?{image}:{error:'This image no longer matches the saved source. Search again or inspect the original document.'})});})
   .catch(error=>{if(!controller.signal.aborted&&current.current===scope&&!isAbort(error))setResult({scope,error:errorMessage(error)});});
  return()=>controller.abort();
 },[scope,retry]);
 const visible=result?.scope===scope?result:null;
 return <aside ref={panel} className="inspector source-reader visual-region-reader" aria-label="Image source reader" aria-busy={!visible} tabIndex={-1}>
  <header className="inspector-top"><span>Image source</span><div className="action-row">{back&&<button className="text-button" onClick={back}>← Back</button>}<button className="icon-button" aria-label="Close image source" onClick={close}><Icon name="close"/></button></div></header>
  <div className="detail-section"><h2>Original image region</h2><p className="field-note">Page {region.source_reference.page} · Version {region.source_reference.version}</p>{region.description&&<p>{region.description}</p>}<p className="field-note">Agent-authored description · unverified. Inspect the original pixels before using it as evidence.</p>
   <div className="reader-view-switch" role="group" aria-label="Image view"><button className="text-button" aria-pressed={view==='region'} onClick={()=>setView('region')}>Image region</button><button className="text-button" aria-pressed={view==='page'} onClick={()=>setView('page')}>Full page</button></div>
   {!visible&&<p role="status">Loading original image…</p>}
   {visible?.error&&<div className="page-preview-error"><p role="alert">{visible.error}</p><button className="button small" onClick={()=>setRetry(value=>value+1)}>Retry image</button></div>}
   {visible?.image&&<div className="page-preview-viewport" role="region" aria-label="Original image pixels" tabIndex={0}><img src={`data:image/png;base64,${visible.image.image.data}`} alt={view==='region'?`Original region: ${region.description}`:`Original PDF page ${region.source_reference.page}`} width={visible.image.width} height={visible.image.height} style={{width:'100%'}} onError={()=>{if(current.current===scope)setResult({scope,error:'The original image could not be displayed.'});}}/></div>}
   <p className="field-note">Region references are not yet supported in collections. You can save the original page instead.</p>
   <button className="button small" onClick={()=>select({type:'document',id:region.source_reference.document_id,page:region.source_reference.page,originalPage:originalPageReference(region.source_reference)})}>Open source reader</button>
  </div>
 </aside>;
}
