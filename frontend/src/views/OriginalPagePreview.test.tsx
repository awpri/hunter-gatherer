import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {ReactElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
const hooks=vi.hoisted(()=>({index:0,slots:[] as any[],effects:[] as (()=>void)[],cleanups:[] as (()=>void)[]}));
vi.mock('react',async()=>({...await vi.importActual<typeof import('react')>('react'),
 useState:(initial:any)=>{const i=hooks.index++;if(!(i in hooks.slots))hooks.slots[i]=typeof initial==='function'?initial():initial;return [hooks.slots[i],(value:any)=>{hooks.slots[i]=typeof value==='function'?value(hooks.slots[i]):value;}];},
 useRef:(initial:any)=>{const i=hooks.index++;return hooks.slots[i]??(hooks.slots[i]={current:initial});},
 useEffect:(effect:()=>unknown,deps:any[])=>{const i=hooks.index++;const previous=hooks.slots[i];if(!previous||deps.some((value,index)=>value!==previous[index])){hooks.slots[i]=deps;hooks.effects.push(()=>{hooks.cleanups[i]?.();const cleanup=effect();hooks.cleanups[i]=typeof cleanup==='function'?cleanup as ()=>void:()=>{};});}},
}));
vi.mock('../lib/libraryApi',()=>({libraryApi:{request:vi.fn(),contextVersion:0},errorMessage:(error:Error)=>error.message,isAbort:(error:Error)=>error.name==='AbortError'}));
import {libraryApi} from '../lib/libraryApi';
import {OriginalPagePreview} from './OriginalPagePreview';
import {imagePacket} from '../lib/pageImageFixtures';
const props:{document:import('../lib/libraryApi').LibraryDocument;includePrivate:boolean;initialPage:number;pageKnown:boolean;active:boolean;onSelectOriginalPage?:ReturnType<typeof vi.fn>;expectedReference?:import('../lib/pageImages').OriginalPageReference;selectionDisabled?:boolean}={document:{document_id:'doc-1',title:'A <source>',version:3,media_type:'application/pdf',created_at:0,is_latest:false,original_content_hash:'a'.repeat(64)},includePrivate:false,initialPage:2,pageKnown:true,active:false};
function elements(node:unknown):ReactElement<any>[]{if(Array.isArray(node))return node.flatMap(elements);if(!node||typeof node!=='object'||!('props' in node))return [];const element=node as ReactElement<any>;return [element,...elements(element.props.children)];}
function render(runEffects=true){hooks.index=0;const tree=OriginalPagePreview(props);if(runEffects)hooks.effects.splice(0).forEach(effect=>effect());return {tree,nodes:elements(tree),html:renderToStaticMarkup(tree)};}
function action(label:string){return render().nodes.find(node=>node.type==='button'&&(node.props['aria-label']===label||node.props.children===label))!.props;}
async function settle(){for(let tick=0;tick<8;tick++)await Promise.resolve();return render();}
async function open(){props.active=true;render();return settle();}
beforeEach(()=>{hooks.cleanups.forEach(cleanup=>cleanup?.());hooks.index=0;hooks.slots=[];hooks.effects=[];hooks.cleanups=[];Object.assign(props,{document:{document_id:'doc-1',title:'A <source>',version:3,media_type:'application/pdf',created_at:0,is_latest:false,original_content_hash:'a'.repeat(64)},includePrivate:false,initialPage:2,pageKnown:true,active:false});props.onSelectOriginalPage=undefined;props.expectedReference=undefined;props.selectionDisabled=false;(libraryApi as any).contextVersion=0;vi.mocked(libraryApi.request).mockReset();vi.mocked(libraryApi.request).mockImplementation(async path=>imagePacket(Number(path.split('/').at(-2))) as any);});
describe('Explicit original-page preview',()=>{
 it('requests no pixels while reading text, then uses the exact version source and private scope',async()=>{
  expect(render().nodes.some(node=>node.type==='img')).toBe(false);expect(libraryApi.request).not.toHaveBeenCalled();props.includePrivate=true;
  const view=await open();expect(libraryApi.request).toHaveBeenCalledWith('documents/doc-1/pages/2/image',{include_sensitive:true,max_side:2048},undefined,undefined,expect.any(AbortSignal));
  expect(view.html).toContain('Version 3');expect(view.html).toContain('Page 2 of 3');expect(view.html).toContain('Original PDF page 2 of 3 from A &lt;source&gt;, version 3');expect(view.html).toContain('does not verify its claims');
 });
 it('bounds page controls and changes only the requested page using read requests',async()=>{
  await open();action('Next original page').onClick();render();await settle();expect(action('Next original page').disabled).toBe(true);
  action('Previous original page').onClick();render();await settle();action('Previous original page').onClick();render();await settle();expect(action('Previous original page').disabled).toBe(true);
  expect(vi.mocked(libraryApi.request).mock.calls.map(call=>call[0])).toEqual(['documents/doc-1/pages/2/image','documents/doc-1/pages/3/image','documents/doc-1/pages/2/image','documents/doc-1/pages/1/image']);
  expect(vi.mocked(libraryApi.request).mock.calls.every(call=>call[2]===undefined)).toBe(true);
 });
 it('zooms and expands with CSS without requesting new pixels; preserves page and zoom on text return',async()=>{
  await open();action('Zoom in').onClick();expect(render().nodes.find(node=>node.type==='img')?.props.style.width).toBe('125%');action('Expand').onClick();expect(render().html).toContain('aria-pressed="true"');
  props.active=false;render();props.active=true;const view=await open();expect(view.nodes.find(node=>node.type==='img')?.props.style.width).toBe('125%');expect(libraryApi.request).toHaveBeenCalledTimes(1);
  action('Fit page width').onClick();expect(render().nodes.find(node=>node.type==='img')?.props.style.width).toBe('100%');
 });
 it('supports keyboard page navigation and zoom while leaving unrelated keys untouched',async()=>{
  await open();const region=()=>render().nodes.find(node=>node.props.className==='page-preview-viewport')!.props;
  const target={},preventDefault=vi.fn();region().onKeyDown({key:'+',target,currentTarget:target,preventDefault});expect(render().nodes.find(node=>node.type==='img')?.props.style.width).toBe('125%');
  region().onKeyDown({key:'ArrowRight',target,currentTarget:target,preventDefault});render();await settle();expect(render().html).toContain('Page 3 of 3');
  preventDefault.mockClear();region().onKeyDown({key:'Tab',target,currentTarget:target,preventDefault});expect(preventDefault).not.toHaveBeenCalled();
 });
 it('shows explicit page-one fallback and loading state without stale pixels during page changes',async()=>{
  props.initialPage=1;props.pageKnown=false;let resolve!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>{resolve=done;}) as any);
  props.active=true;const view=render();expect(view.html).toContain('starting at page 1');expect(view.html).toContain('Loading original page 1');expect(view.nodes.some(node=>node.type==='img')).toBe(false);
  resolve(imagePacket(1));await settle();action('Next original page').onClick();const pending=render();expect(pending.nodes.some(node=>node.type==='img')).toBe(false);expect(pending.html).toContain('Loading original page 2');
 });
 it.each(['privacy','version','document','page'])('hides pixels synchronously on %s change and ignores transports which ignore abort',async change=>{
  props.includePrivate=true;await open();let resolve!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>{resolve=done;}) as any);
  action('Next original page').onClick();render();const signal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;const late=resolve;
  if(change==='privacy')props.includePrivate=false;if(change==='version')props.document={...props.document,version:4};if(change==='document')props.document={...props.document,document_id:'doc-other'};if(change==='page')props.initialPage=1;
  expect(render(false).nodes.some(node=>node.type==='img')).toBe(false);hooks.effects.splice(0).forEach(effect=>effect());expect(signal.aborted).toBe(true);
  late(imagePacket(3));await settle();expect(render().nodes.some(node=>node.type==='img')).toBe(false);
 });
 it('ignores superseded page completion and stale image decode errors',async()=>{
  const loaded=await open(),oldImage=loaded.nodes.find(node=>node.type==='img')!;
  const pending=new Map<number,(value:any)=>void>();vi.mocked(libraryApi.request).mockImplementation(path=>new Promise(done=>pending.set(Number(path.split('/').at(-2)),done)) as any);
  action('Next original page').onClick();render();const oldSignal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;
  action('Previous original page').onClick();render();expect(oldSignal.aborted).toBe(true);expect(render().nodes.some(node=>node.type==='img')).toBe(true);
  action('Previous original page').onClick();render();pending.get(1)!(imagePacket(1));await settle();pending.get(3)!(imagePacket(3));await settle();oldImage.props.onError();
  expect(render().html).toContain('Page 1 of 3');expect(render().html).not.toContain('could not be displayed');
 });
 it('aborts on unmount and text-view switches and ignores late failures',async()=>{
  let reject!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise((_done,fail)=>{reject=fail;}) as any);props.active=true;render();const signal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;
  props.active=false;render();expect(signal.aborted).toBe(true);reject(new Error('PRIVATE FAILURE'));await settle();expect(render().html).not.toContain('PRIVATE FAILURE');
  props.active=true;render();const unmountSignal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;hooks.cleanups.forEach(cleanup=>cleanup?.());expect(unmountSignal.aborted).toBe(true);
 });
 it('reports endpoint errors and retries the same page; malformed identity displays no pixels',async()=>{
  vi.mocked(libraryApi.request).mockRejectedValue(new Error('Original file is unavailable.'));expect((await open()).html).toContain('Original file is unavailable.');
  vi.mocked(libraryApi.request).mockResolvedValue(imagePacket(2));action('Retry page').onClick();render();expect((await settle()).nodes.some(node=>node.type==='img')).toBe(true);
  render().nodes.find(node=>node.type==='img')!.props.onError();expect(render().html).toContain('could not be displayed');
  vi.mocked(libraryApi.request).mockResolvedValue({...imagePacket(2),version:4});action('Retry page').onClick();render();const invalid=await settle();expect(invalid.html).toContain('did not match this source version');expect(invalid.nodes.some(node=>node.type==='img')).toBe(false);
 });
});


describe('Exact original pages selected for a collection',()=>{
 const label='Select page for collection';
 function decode(){render().nodes.find(node=>node.type==='img')!.props.onLoad();return action(label);}
 it('requires a validated and displayed response, then sends only its exact four reference fields',async()=>{
  props.onSelectOriginalPage=vi.fn();props.active=true;render();expect(action(label).disabled).toBe(true);await settle();expect(action(label).disabled).toBe(true);
  action(label).onClick();expect(props.onSelectOriginalPage).not.toHaveBeenCalled();expect(decode().disabled).toBe(false);
  action(label).onClick();expect(props.onSelectOriginalPage).toHaveBeenCalledWith(imagePacket(2).source_reference);
  expect(Object.keys(props.onSelectOriginalPage.mock.calls[0][0])).toEqual(['document_id','version','original_content_hash','page']);
  expect(vi.mocked(libraryApi.request).mock.calls.every(call=>call[2]===undefined)).toBe(true);
 });
 it('disables selection while the next page loads and on failure, and rejects a stale action or decode',async()=>{
  props.onSelectOriginalPage=vi.fn();await open();decode();const oldAction=action(label),oldImage=render().nodes.find(node=>node.type==='img')!;
  let reject!:(value:unknown)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise((_resolve,fail)=>{reject=fail;}) as any);
  action('Next original page').onClick();render();expect(action(label).disabled).toBe(true);oldAction.onClick();oldImage.props.onLoad();expect(props.onSelectOriginalPage).not.toHaveBeenCalled();
  reject(new Error('Page unavailable'));await settle();expect(action(label).disabled).toBe(true);expect(render().html).toContain('Page unavailable');
 });
 it.each(['privacy','hash','version','context','inactive'])('rejects an old selection synchronously after %s changes',async change=>{
  props.onSelectOriginalPage=vi.fn();await open();decode();const old=action(label);
  if(change==='privacy')props.includePrivate=true;if(change==='hash')props.document={...props.document,original_content_hash:'b'.repeat(64)};
  if(change==='version')props.document={...props.document,version:4};if(change==='context')(libraryApi as any).contextVersion++;
  if(change==='inactive')props.active=false;
  render(false);old.onClick();expect(props.onSelectOriginalPage).not.toHaveBeenCalled();expect(action(label).disabled).toBe(true);
 });
 it('does not retarget a saved page when the expected hash differs from document/image metadata',async()=>{
  props.onSelectOriginalPage=vi.fn();props.expectedReference={...imagePacket(2).source_reference,original_content_hash:'b'.repeat(64)};
  const view=await open();expect(view.nodes.some(node=>node.type==='img')).toBe(false);expect(view.html).toContain('saved original page is unavailable');expect(action(label).disabled).toBe(true);
 });
 it('does not select images with noncanonical hashes or a failed decode',async()=>{
  props.onSelectOriginalPage=vi.fn();vi.mocked(libraryApi.request).mockResolvedValue({...imagePacket(),original_content_hash:'HASH',source_reference:{...imagePacket().source_reference,original_content_hash:'HASH'}});
  await open();expect(action(label).disabled).toBe(true);expect(render().nodes.some(node=>node.type==='img')).toBe(false);
  vi.mocked(libraryApi.request).mockResolvedValue(imagePacket());action('Retry page').onClick();render();await settle();decode();render().nodes.find(node=>node.type==='img')!.props.onError();render();expect(action(label).disabled).toBe(true);
 });
});
