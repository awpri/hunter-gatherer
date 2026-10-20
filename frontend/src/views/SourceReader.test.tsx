import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {ReactElement} from 'react';
const hooks=vi.hoisted(()=>({index:0,slots:[] as any[],effects:[] as (()=>unknown)[],cleanups:[] as (()=>void)[]}));
vi.mock('react',async()=>({...await vi.importActual<typeof import('react')>('react'),
 useState:(initial:any)=>{const i=hooks.index++;if(!(i in hooks.slots))hooks.slots[i]=typeof initial==='function'?initial():initial;return [hooks.slots[i],(value:any)=>{hooks.slots[i]=typeof value==='function'?value(hooks.slots[i]):value;}];},
 useRef:(initial:any)=>{const i=hooks.index++;return hooks.slots[i]??(hooks.slots[i]={current:initial});},
 useLayoutEffect:()=>{},
 useEffect:(effect:()=>unknown,deps:any[])=>{const i=hooks.index++;const previous=hooks.slots[i];if(!previous||deps.some((value,index)=>value!==previous[index])){hooks.slots[i]=deps;hooks.effects.push(()=>{hooks.cleanups[i]?.();const cleanup=effect();hooks.cleanups[i]=typeof cleanup==='function'?cleanup as ()=>void:()=>{};});}},
}));
vi.mock('../lib/libraryApi',()=>({libraryApi:{request:vi.fn()},errorMessage:(error:Error)=>error.message,isAbort:()=>false}));
import {libraryApi} from '../lib/libraryApi';
import {SourceReader} from './SourceReader';
import {SourceText} from './SourceText';
import {TableCellInspector} from './TableCellInspector';
import {OriginalPagePreview} from './OriginalPagePreview';
import {packet} from './tableCellFixtures';
function elements(node:unknown):ReactElement<any>[] {if(Array.isArray(node))return node.flatMap(elements);if(!node||typeof node!=='object'||!('props' in node))return [];const element=node as ReactElement<any>;return [element,...elements(element.props.children)];}
const props={selection:{type:'document' as const,id:'source-1'} as import('./LibraryInspector').Selection,includePrivate:false,select:vi.fn(),close:vi.fn(),changed:vi.fn(),onSelectOriginalPage:vi.fn()};
function render(){hooks.index=0;const tree=SourceReader(props);hooks.effects.splice(0).forEach(effect=>effect());return elements(tree);}
function button(nodes:ReactElement<any>[],text:string){return nodes.find(node=>node.type==='button'&&node.props.children===text);}
async function load(media_type='application/pdf',is_latest=true,warnings:string[]=[],outlineIsLatest=is_latest){
 vi.mocked(libraryApi.request).mockImplementation(async(path)=>{
  if(path.startsWith('chunks/'))return {...packet(),chunks:packet().chunks.map(chunk=>({...chunk,document_id:'source-1'}))} as any;
  if(path.endsWith('/proposal-progress'))return {started:false,counts:{pending:0,approved:0,rejected:0}} as any;
  if(path.endsWith('/outline'))return {sections:[],total_sections:0,next_offset:null,is_latest:outlineIsLatest} as any;
  return {document_id:'source-1',title:'Source fixture',version:1,original_content_hash:'a'.repeat(64),created_at:0,media_type,is_latest,warnings,passages:[]} as any;
 });
 render();for(let tick=0;tick<8;tick++)await Promise.resolve();return render();
}
beforeEach(()=>{hooks.cleanups.forEach(cleanup=>cleanup?.());hooks.cleanups=[];props.selection={type:'document',id:'source-1'};props.includePrivate=false;hooks.index=0;hooks.slots=[];hooks.effects=[];vi.mocked(libraryApi.request).mockReset();props.select.mockReset();props.changed.mockReset();props.onSelectOriginalPage.mockReset();vi.stubGlobal('requestAnimationFrame',(fn:()=>void)=>fn());vi.stubGlobal('document',{getSelection:()=>null});});
describe('Source reader extraction controls',()=>{
 it('offers PDF recovery without extraction warnings, outside the notes disclosure',async()=>{
  const nodes=await load();expect(button(nodes,'Re-extract source')?.props.disabled).toBe(false);expect(button(nodes,'Recover text & layout')?.props.disabled).toBe(false);
  expect(nodes.some(node=>node.type==='summary'&&String(node.props.children).includes('Extraction notes'))).toBe(false);
  expect(nodes.some(node=>node.type==='p'&&String(node.props.children).includes('Creates a new version'))).toBe(true);
 });
 it('offers native extraction without OCR for non-PDF sources',async()=>{
  const nodes=await load('text/plain');expect(button(nodes,'Re-extract source')).toBeDefined();expect(button(nodes,'Recover text & layout')).toBeUndefined();expect(nodes.some(node=>node.type==='select')).toBe(false);
 });
 it('preserves extraction warnings and version history and disables mutations on historical sources',async()=>{
  const nodes=await load('application/pdf',false,['Compare with the original.']);expect(button(nodes,'Version history')).toBeDefined();expect(nodes.some(node=>node.props.children==='Compare with the original.')).toBe(true);
  expect(button(nodes,'Re-extract source')?.props.disabled).toBe(true);expect(button(nodes,'Recover text & layout')?.props.disabled).toBe(true);
 });
 it('binds the historical label and controls to the displayed source rather than the outline snapshot',async()=>{
  let nodes=await load('application/pdf',false,[],true);
  expect(nodes.some(node=>node.type==='p'&&Array.isArray(node.props.children)&&node.props.children.includes(' · Historical source'))).toBe(true);
  expect(button(nodes,'Propose excerpts')?.props.disabled).toBe(true);
  hooks.index=0;hooks.slots=[];hooks.effects=[];
  nodes=await load('application/pdf',true,[],false);
  expect(nodes.some(node=>node.type==='p'&&Array.isArray(node.props.children)&&node.props.children.includes(' · Historical source'))).toBe(false);
  expect(button(nodes,'Propose excerpts')?.props.disabled).toBe(false);
  expect(button(nodes,'Re-extract source')?.props.disabled).toBe(false);
 });
 it('uses the shared citation renderer for the read-file path',async()=>{
  const nodes=await load();
  const source=nodes.find(node=>node.type===SourceText);
  expect(source?.props.doc.document_id).toBe('source-1');
  expect(source?.props.select).toBe(props.select);
 });
 it('reports OCR status, guards duplicate mutations, and opens the new source version',async()=>{
  let nodes=await load();let resolve!:(value:any)=>void;const pending=new Promise(resolvePending=>{resolve=resolvePending;});vi.mocked(libraryApi.request).mockImplementation(()=>pending as any);
  const start=button(nodes,'Recover text & layout')!.props.onClick;const operation=start();start();nodes=render();
  expect(button(nodes,'Recovering text & layout…')?.props.disabled).toBe(true);expect(button(nodes,'Propose excerpts')?.props.disabled).toBe(true);expect(button(nodes,'Proposing…')).toBeUndefined();
  expect(nodes.some(node=>node.props.role==='status'&&node.props.children==='Recovering text & layout…')).toBe(true);
  expect(vi.mocked(libraryApi.request).mock.calls.filter(call=>call[0].endsWith('/reextract'))).toHaveLength(1);
  expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[2]).toEqual({backend:'docling',include_sensitive:false});
  resolve({document_id:'source-2'});await operation;expect(props.select).toHaveBeenCalledWith({type:'document',id:'source-2'});expect(button(render(),'Recover text & layout')?.props.disabled).toBe(false);
 });
 it('clears recovery status after errors so extraction can be retried',async()=>{
  let nodes=await load();vi.mocked(libraryApi.request).mockRejectedValue(new Error('Offline cache incomplete'));
  await button(nodes,'Recover text & layout')!.props.onClick();nodes=render();expect(button(nodes,'Recover text & layout')?.props.disabled).toBe(false);expect(button(nodes,'Recovering text & layout…')).toBeUndefined();expect(nodes.some(node=>node.props.message==='Offline cache incomplete')).toBe(true);
 });
 it('sends explicit Tesseract for recovery and retains the native request body',async()=>{
  let nodes=await load();const choice=nodes.find(node=>node.type==='select')!;
  expect(choice.props.value).toBe('easyocr');expect(choice.props.disabled).toBe(false);
  expect(nodes.some(node=>node.type==='label'&&elements(node).some(child=>child.type==='select'))).toBe(true);
  choice.props.onChange({target:{value:'tesseract'}});nodes=render();
  vi.mocked(libraryApi.request).mockResolvedValue({document_id:'source-2'} as any);
  await button(nodes,'Recover text & layout')!.props.onClick();
  expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[2]).toEqual({backend:'docling',include_sensitive:false,ocr_engine:'tesseract'});
  await button(render(),'Re-extract source')!.props.onClick();
  expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[2]).toEqual({backend:'native',include_sensitive:false});
 });
 it('resets the choice on document change and blocks mutation while another document is displayed',async()=>{
  let nodes=await load();nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  props.selection={type:'document',id:'source-other'};nodes=render();
  expect(nodes.find(node=>node.type==='select')).toBeUndefined();
  expect(button(nodes,'Recover text & layout')).toBeUndefined();
  props.selection={type:'document',id:'source-1'};render();for(let tick=0;tick<8;tick++)await Promise.resolve();nodes=render();
  expect(nodes.find(node=>node.type==='select')?.props.value).toBe('easyocr');
 });
 it('disables OCR selection on historical sources and while recovery runs',async()=>{
  let nodes=await load('application/pdf',false);expect(nodes.find(node=>node.type==='select')?.props.disabled).toBe(true);
  hooks.index=0;hooks.slots=[];hooks.effects=[];nodes=await load();
  let resolve!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>{resolve=done;}) as any);
  const operation=button(nodes,'Recover text & layout')!.props.onClick();
  expect(render().find(node=>node.type==='select')?.props.disabled).toBe(true);resolve({});await operation;
 });
 it('forwards private scope with the selected engine',async()=>{
  props.includePrivate=true;let nodes=await load();nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  vi.mocked(libraryApi.request).mockResolvedValue({} as any);await button(nodes,'Recover text & layout')!.props.onClick();
  expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[2]).toEqual({backend:'docling',include_sensitive:true,ocr_engine:'tesseract'});
 });
 it('shows only recorded OCR provenance and does not infer an engine for old sources',async()=>{
  let nodes=await load();expect(nodes.some(node=>node.props.children==='Saved OCR engine')).toBe(false);
  hooks.index=0;hooks.slots=[];hooks.effects=[];
  vi.mocked(libraryApi.request).mockResolvedValue({document_id:'source-1',title:'Historical',version:1,created_at:0,media_type:'application/pdf',is_latest:false,passages:[],sections:[],extraction:{provenance:{config:{ocr_engine:'TesseractCliOcrOptions'}}}} as any);
  render();for(let tick=0;tick<8;tick++)await Promise.resolve();nodes=render();
  expect(nodes.some(node=>node.type==='dt'&&node.props.children==='Saved OCR engine')).toBe(true);
  expect(nodes.some(node=>node.type==='dd'&&node.props.children==='Tesseract')).toBe(true);
 });

 it('ignores a completed recovery after the user selects another source',async()=>{
  let nodes=await load();nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  let resolve!:(value:any)=>void;const pending=new Promise(done=>{resolve=done;});
  vi.mocked(libraryApi.request).mockImplementation(path=>path.endsWith('/reextract')?pending as any:Promise.resolve({}) as any);
  const operation=button(nodes,'Recover text & layout')!.props.onClick();props.selection={type:'document',id:'source-other'};render();
  resolve({document_id:'recovered-old-source'});await operation;
  expect(props.select).not.toHaveBeenCalled();expect(props.changed).not.toHaveBeenCalled();
 });

 it('offers preserve orientation only for Tesseract and sends it only for recovery',async()=>{
  let nodes=await load();expect(nodes.some(node=>node.type==='input'&&node.props.type==='checkbox')).toBe(false);
  nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  const checkbox=nodes.find(node=>node.type==='input'&&node.props.type==='checkbox')!;
  expect(checkbox.props.checked).toBe(false);expect(checkbox.props.disabled).toBe(false);
  expect(nodes.some(node=>node.props.children==='Use for scans whose pages are already upright.')).toBe(true);
  checkbox.props.onChange({target:{checked:true}});nodes=render();
  vi.mocked(libraryApi.request).mockResolvedValue({} as any);
  await button(nodes,'Recover text & layout')!.props.onClick();
  expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[2]).toEqual({backend:'docling',include_sensitive:false,ocr_engine:'tesseract',ocr_orientation:'preserve'});
  await button(render(),'Re-extract source')!.props.onClick();
  expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[2]).toEqual({backend:'native',include_sensitive:false});
 });
 it('resets preserve orientation when switching engines or sources',async()=>{
  let nodes=await load();nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  nodes.find(node=>node.type==='input')!.props.onChange({target:{checked:true}});nodes=render();
  nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'easyocr'}});nodes=render();
  expect(nodes.some(node=>node.type==='input')).toBe(false);
  nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  expect(nodes.find(node=>node.type==='input')?.props.checked).toBe(false);
  nodes.find(node=>node.type==='input')!.props.onChange({target:{checked:true}});render();
  props.selection={type:'document',id:'source-other'};nodes=render();
  expect(nodes.some(node=>node.type==='input')).toBe(false);expect(nodes.find(node=>node.type==='select')).toBeUndefined();
 });
 it('disables preserve orientation during recovery and for historical sources',async()=>{
  let nodes=await load('application/pdf',false);nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  expect(nodes.find(node=>node.type==='input')?.props.disabled).toBe(true);
  hooks.index=0;hooks.slots=[];hooks.effects=[];nodes=await load();
  nodes.find(node=>node.type==='select')!.props.onChange({target:{value:'tesseract'}});nodes=render();
  let resolve!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>{resolve=done;}) as any);
  const operation=button(nodes,'Recover text & layout')!.props.onClick();
  expect(render().find(node=>node.type==='input')?.props.disabled).toBe(true);resolve({});await operation;
 });

 it('shows saved orientation only when recorded with Tesseract provenance',async()=>{
  let nodes=await load();expect(nodes.some(node=>node.props.children==='Saved page orientation')).toBe(false);
  hooks.index=0;hooks.slots=[];hooks.effects=[];
  vi.mocked(libraryApi.request).mockResolvedValue({document_id:'source-1',title:'Historical',version:1,created_at:0,media_type:'application/pdf',is_latest:false,passages:[],sections:[],extraction:{provenance:{config:{ocr_engine:'TesseractCliOcrOptions',ocr_orientation:'preserve'}}}} as any);
  render();for(let tick=0;tick<8;tick++)await Promise.resolve();nodes=render();
  expect(nodes.some(node=>node.type==='dt'&&node.props.children==='Saved page orientation')).toBe(true);
  expect(nodes.some(node=>node.type==='dd'&&node.props.children==='Keep page orientation')).toBe(true);
  expect(nodes.find(node=>node.type==='select')?.props.value).toBe('easyocr');
 });

});


describe('Source reader table integration and scope isolation',()=>{
 it('keeps normal file reading unchanged and offers inspection only for exact chunk selections',async()=>{
  expect((await load()).some(node=>node.type===TableCellInspector)).toBe(false);
  props.selection={type:'document',id:'source-1',chunk:'chunk-1'};
  const nodes=await load();const inspector=nodes.find(node=>node.type===TableCellInspector)!;
  expect(inspector.props.chunkId).toBe('chunk-1');expect(inspector.props.includePrivate).toBe(false);
  expect(libraryApi.request).not.toHaveBeenCalledWith(expect.anything(),expect.objectContaining({table_cell_offset:expect.anything()}),expect.anything(),expect.anything(),expect.anything());
 });
 it('offers an attachment download and previews the exact selected source anchor page',async()=>{
  props.selection={type:'document',id:'doc-1',chunk:'chunk-1',page:99};
  vi.mocked(libraryApi.request).mockImplementation(async(path)=>path.startsWith('chunks/')?packet():path.endsWith('/outline')?{sections:[]}:path.endsWith('/proposal-progress')?{counts:{}}:{document_id:'doc-1',version:3,title:'Historical source',media_type:'application/pdf',is_latest:false});
  render();for(let tick=0;tick<8;tick++)await Promise.resolve();
  const link=render().find(node=>node.type==='a'&&node.props.children==='Download original')!;
  expect(link.props.href).toBe('/api/library/documents/doc-1/original?include_sensitive=false');
  const preview=render().find(node=>node.type===OriginalPagePreview)!;expect(preview.props.document.document_id).toBe('doc-1');expect(preview.props.document.version).toBe(3);expect(preview.props.initialPage).toBe(7);expect(preview.props.active).toBe(false);
 });
 it('aborts ordinary private reads and ignores results even when mocks ignore abort',async()=>{
  props.includePrivate=true;let resolve!:(value:any)=>void;
  const pending=new Promise(done=>{resolve=done;});vi.mocked(libraryApi.request).mockImplementation(()=>pending as any);
  render();const signals=vi.mocked(libraryApi.request).mock.calls.map(call=>call[4]);
  props.includePrivate=false;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(()=>{}) as any);render();
  expect(signals.every(signal=>signal?.aborted)).toBe(true);
  resolve({document_id:'source-1',title:'PRIVATE TITLE',version:1,sections:[],counts:{}});for(let tick=0;tick<8;tick++)await Promise.resolve();
  expect(render().some(node=>node.props.children==='PRIVATE TITLE')).toBe(false);
 });
 it('hides loaded private source content before effects and cancels history reads',async()=>{
  props.includePrivate=true;let nodes=await load();let resolve!:(value:any)=>void;
  vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>{resolve=done;}) as any);
  const history=button(nodes,'Version history')!.props.onClick();const resolveHistory=resolve;const signal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;
  props.includePrivate=false;hooks.index=0;const tree=SourceReader(props);
  expect(elements(tree).some(node=>node.props.children==='Source fixture')).toBe(false);
  hooks.effects.splice(0).forEach(effect=>effect());expect(signal.aborted).toBe(true);
  resolveHistory({versions:[{document_id:'private-version',version:7,title:'PRIVATE HISTORY'}]});await history;
  expect(render().some(node=>String(node.props.children).includes('PRIVATE HISTORY'))).toBe(false);
 });
});


describe('Source reader original-page view',()=>{
 it('preserves text position and selection without reloading or creating extracts when changing views',async()=>{
  props.selection={type:'document',id:'source-1',start:200,page:2};let nodes=await load();const panel={scrollTop:417,contains:()=>false};(nodes[0] as any).ref.current=panel;
  const requests=vi.mocked(libraryApi.request).mock.calls.length;button(nodes,'Original page')!.props.onClick();nodes=render();expect(nodes.find(node=>node.type===OriginalPagePreview)?.props.active).toBe(true);
  expect(nodes.some(node=>node.type===SourceText)).toBe(true);expect(nodes.some(node=>node.type==='div'&&node.props.hidden===true)).toBe(true);
  panel.scrollTop=90;button(nodes,'Source text')!.props.onClick();nodes=render();expect(panel.scrollTop).toBe(417);expect(nodes.find(node=>node.type===OriginalPagePreview)?.props.active).toBe(false);
  expect(props.selection).toEqual({type:'document',id:'source-1',start:200,page:2});expect(props.select).not.toHaveBeenCalled();expect(props.changed).not.toHaveBeenCalled();expect(libraryApi.request).toHaveBeenCalledTimes(requests);
 });
 it('offers original pages only for PDFs and makes unknown-page fallback explicit to the preview',async()=>{
  let nodes=await load('text/plain');expect(button(nodes,'Original page')).toBeUndefined();expect(nodes.some(node=>node.type===OriginalPagePreview)).toBe(false);
  hooks.cleanups.forEach(cleanup=>cleanup?.());hooks.index=0;hooks.slots=[];hooks.effects=[];hooks.cleanups=[];nodes=await load();const preview=nodes.find(node=>node.type===OriginalPagePreview)!;
  expect(preview.props.initialPage).toBe(1);expect(preview.props.pageKnown).toBe(false);expect(preview.props.active).toBe(false);
 });
});


describe('Table anchor opens inline original-page view',()=>{
 it('opens the callback page and preserves it across text switches without changing source selection',async()=>{
  props.selection={type:'document',id:'source-1',chunk:'chunk-1',start:40};let nodes=await load();const requests=vi.mocked(libraryApi.request).mock.calls.length;
  nodes.find(node=>node.type===TableCellInspector)!.props.onOriginalPage(7);nodes=render();let preview=nodes.find(node=>node.type===OriginalPagePreview)!;expect(preview.props.active).toBe(true);expect(preview.props.initialPage).toBe(7);expect(preview.props.pageKnown).toBe(true);
  button(nodes,'Source text')!.props.onClick();nodes=render();button(nodes,'Original page')!.props.onClick();preview=render().find(node=>node.type===OriginalPagePreview)!;expect(preview.props.initialPage).toBe(7);expect(preview.props.active).toBe(true);
  expect(props.select).not.toHaveBeenCalled();expect(props.changed).not.toHaveBeenCalled();expect(libraryApi.request).toHaveBeenCalledTimes(requests);
 });
});


describe('Exact saved original page and empty OCR reader',()=>{
 const ref={document_id:'source-1',version:1,original_content_hash:'a'.repeat(64),page:3};
 it('opens a saved page directly on its historical source without chunk requests or recovered text',async()=>{
  props.selection={type:'document',id:ref.document_id,page:ref.page,originalPage:ref};
  vi.mocked(libraryApi.request).mockImplementation(async path=>path.endsWith('/proposal-progress')?{counts:{}}:path.endsWith('/outline')?{sections:[],total_sections:0}:{...ref,title:'Empty OCR scan',media_type:'application/pdf',is_latest:false,passages:[],total_chars:0} as any);
  render();for(let tick=0;tick<8;tick++)await Promise.resolve();const nodes=render(),preview=nodes.find(node=>node.type===OriginalPagePreview)!;
  expect(preview.props.active).toBe(true);expect(preview.props.initialPage).toBe(3);expect(preview.props.expectedReference).toEqual(ref);expect(preview.props.document.is_latest).toBe(false);
  preview.props.onSelectOriginalPage(ref);expect(props.onSelectOriginalPage).toHaveBeenCalledWith(ref,expect.objectContaining({title:'Empty OCR scan',total_chars:0}));
  expect(vi.mocked(libraryApi.request).mock.calls.some(call=>call[0].startsWith('chunks/'))).toBe(false);expect(vi.mocked(libraryApi.request).mock.calls.every(call=>call[2]===undefined)).toBe(true);
 });
 it.each([{version:2},{original_content_hash:'b'.repeat(64)}])('leaves the saved page explicitly unavailable if source metadata changes (%j)',async change=>{
  props.selection={type:'document',id:ref.document_id,page:ref.page,originalPage:ref};
  vi.mocked(libraryApi.request).mockImplementation(async path=>path.endsWith('/proposal-progress')?{counts:{}}:path.endsWith('/outline')?{sections:[],total_sections:0}:{...ref,...change,title:'Replacement source',media_type:'application/pdf'} as any);
  render();for(let tick=0;tick<8;tick++)await Promise.resolve();const nodes=render();expect(nodes.some(node=>node.type===OriginalPagePreview)).toBe(false);expect(nodes.some(node=>node.props.message==='The saved original page is unavailable for this source version.')).toBe(true);
 });
 it('opens image-only Library files through the page switch and rejects callbacks after navigation',async()=>{
  let nodes=await load();expect(nodes.find(node=>node.type===SourceText)?.props.doc.passages).toEqual([]);button(nodes,'Original page')!.props.onClick();nodes=render();const preview=nodes.find(node=>node.type===OriginalPagePreview)!;
  expect(preview.props.active).toBe(true);preview.props.onSelectOriginalPage({...ref,version:2});preview.props.onSelectOriginalPage({...ref,original_content_hash:'b'.repeat(64)});expect(props.onSelectOriginalPage).not.toHaveBeenCalled();preview.props.onSelectOriginalPage(ref);expect(props.onSelectOriginalPage).toHaveBeenCalledTimes(1);
  props.selection={type:'document',id:'source-other'};render();preview.props.onSelectOriginalPage(ref);expect(props.onSelectOriginalPage).toHaveBeenCalledTimes(1);
 });
});


describe('Original-page reopen uses the real document catalog hash contract',()=>{
 const reference={document_id:'source-1',version:3,original_content_hash:'a'.repeat(64),page:2};
 it.each(['content_hash','missing'])('opens exact image preview with a %s metadata hash and keeps the expected reference',async field=>{
  props.selection={type:'document',id:reference.document_id,page:2,originalPage:reference};
  vi.mocked(libraryApi.request).mockImplementation(async path=>path.endsWith('/proposal-progress')?{counts:{}}:path.endsWith('/outline')?{sections:[],total_sections:0}:{document_id:'source-1',version:3,title:'Real catalog scan',media_type:'application/pdf',passages:[],...(field==='content_hash'?{content_hash:reference.original_content_hash}:{})} as any);
  render();for(let tick=0;tick<8;tick++)await Promise.resolve();const preview=render().find(node=>node.type===OriginalPagePreview)!;
  expect(preview).toBeDefined();expect(preview.props.active).toBe(true);expect(preview.props.expectedReference).toEqual(reference);expect(preview.props.initialPage).toBe(2);
  preview.props.onSelectOriginalPage(reference);expect(props.onSelectOriginalPage).toHaveBeenCalledWith(reference,expect.objectContaining({title:'Real catalog scan'}));
 });
 it('rejects a conflicting real content_hash before showing the preview',async()=>{
  props.selection={type:'document',id:reference.document_id,page:2,originalPage:reference};
  vi.mocked(libraryApi.request).mockImplementation(async path=>path.endsWith('/proposal-progress')?{counts:{}}:path.endsWith('/outline')?{sections:[],total_sections:0}:{document_id:'source-1',version:3,title:'Changed scan',media_type:'application/pdf',content_hash:'b'.repeat(64)} as any);
  render();for(let tick=0;tick<8;tick++)await Promise.resolve();expect(render().some(node=>node.type===OriginalPagePreview)).toBe(false);
 });
});


describe('Saved original-file identity',()=>{
 it('opens matching metadata without requiring a PDF page',async()=>{
  props.selection={type:'document',id:'source-1',originalFile:{document_id:'source-1',version:1,original_content_hash:'a'.repeat(64)}};
  const nodes=await load('application/octet-stream');
  expect(nodes.some(node=>node.type===OriginalPagePreview)).toBe(false);
  expect(nodes.some(node=>node.props.message==='The saved original file is unavailable for this source version.')).toBe(false);
 });
 it('refuses mismatched bytes behind a saved file reference',async()=>{
  props.selection={type:'document',id:'source-1',originalFile:{document_id:'source-1',version:1,original_content_hash:'b'.repeat(64)}};
  const nodes=await load('application/octet-stream');
  expect(nodes.some(node=>node.props.message==='The saved original file is unavailable for this source version.')).toBe(true);
 });
});

describe('Reader navigation remains independent from collection actions',()=>{
 it('passes back and close through without saving, changing selection or reloading evidence',async()=>{
  await load('text/plain');const requests=vi.mocked(libraryApi.request).mock.calls.length;const back=vi.fn();const close=vi.fn();
  hooks.index=0;const nodes=elements(SourceReader({...props,back,close}));
  button(nodes,'← Back')!.props.onClick({detail:0});expect(back).toHaveBeenCalledOnce();
  nodes.find(node=>node.type==='button'&&node.props['aria-label']==='Close source reader')!.props.onClick({detail:0});expect(close).toHaveBeenCalledOnce();
  expect(props.select).not.toHaveBeenCalled();expect(props.changed).not.toHaveBeenCalled();expect(libraryApi.request).toHaveBeenCalledTimes(requests);
 });
});

 describe('Source reader visible history',()=>{
 it('shows version links outside hidden text content while original page mode is active',async()=>{
  let nodes=await load();
  button(nodes,'Original page')!.props.onClick();nodes=render();
  expect(nodes.find(node=>node.type===OriginalPagePreview)?.props.active).toBe(true);
  vi.mocked(libraryApi.request).mockResolvedValue({versions:[{document_id:'source-old',version:1,title:'Earlier source'}]} as any);
  await button(nodes,'Version history')!.props.onClick();nodes=render();
  const versionLink=nodes.find(node=>node.type==='button'&&elements(node).some(child=>Array.isArray(child.props.children)&&child.props.children.includes('Earlier source')));
  expect(versionLink).toBeDefined();
  const hidden=nodes.filter(node=>node.props.hidden===true);
  expect(hidden.some(node=>elements(node).includes(versionLink!))).toBe(false);
  versionLink!.props.onClick();expect(props.select).toHaveBeenCalledWith({type:'document',id:'source-old'});
 });
});
