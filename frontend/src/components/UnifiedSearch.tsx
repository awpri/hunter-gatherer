import {useEffect, useId, useRef, useState, type ReactNode, type RefObject} from 'react';
import {Icon} from '../views/LibraryShared';
import {matchSearchCommands, nextSearchIndex} from './unifiedSearchModel';
import './UnifiedSearch.css';
export interface SearchCommand {id:string; label:string; description?:string; section?:string; icon?:ReactNode; keywords?:string[]; onSelect:()=>void}
export type SearchMode = 'ask'|'records'|'commands';
interface Props {inputRef:RefObject<HTMLInputElement>; query:string; onQuery:(value:string)=>void; commands:SearchCommand[]; groupLabel?:string; onClearGroup:()=>void; onSearch:(value:string)=>void;onGather?:(value:string)=>void;mode?:SearchMode;onModeChange?:(mode:SearchMode)=>void}
export function UnifiedSearch({inputRef,query,onQuery,commands,groupLabel,onClearGroup,onSearch,onGather,mode='ask',onModeChange}:Props) {
  const [open,setOpen]=useState(false); const [active,setActive]=useState(0); const root=useRef<HTMLDivElement>(null); const id=useId();
  const trimmed=query.trim();
  const matches=matchSearchCommands(commands,query,!!onGather&&mode!=='commands');
  const records={id:'records',label:trimmed?`Find records for “${trimmed}”`:'Browse all records',description:groupLabel?`Within ${groupLabel}`:'Browse memories and files',section:'Records',icon:<Icon name="search" size={15}/>,onSelect:()=>onSearch(trimmed)};
  const ask=onGather?{id:'evidence-query',label:trimmed?`Ask library: “${trimmed}”`:'Ask your library',description:'Find quotations and inspect their sources',section:'Ask',icon:<Icon name="knowledge" size={15}/>,onSelect:()=>onGather(trimmed)}:null;
  const contextual=mode==='commands'?[]:mode==='records'?[records,...(ask?[ask]:[])]:[...(ask?[ask]:[]),records];
  const rows=[...contextual,...matches];
  const selected=Math.min(active,rows.length-1);
  useEffect(()=>{const close=(event:PointerEvent)=>{if(event.target instanceof Node&&!root.current?.contains(event.target))setOpen(false);};document.addEventListener('pointerdown',close);return()=>document.removeEventListener('pointerdown',close);},[]);
  useEffect(()=>{if(open&&selected>=0)document.getElementById(`${id}-${selected}`)?.scrollIntoView({block:'nearest'});},[id,selected,open]);
  function choose(index:number){const row=rows[index];if(!row)return;setOpen(false);setActive(0);if(index>=contextual.length)onQuery('');row.onSelect();}
  const placeholder=mode==='commands'?'Find a command or group…':mode==='records'?'Find memories and files…':'Ask your library…';
  return <div ref={root} className="search-box unified-search" onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget as Node|null))setOpen(false);}}>
    {onModeChange&&<span className="search-mode-control" data-mode={mode}><select className="search-mode" aria-label="Search mode" value={mode} onChange={event=>{onModeChange(event.target.value as SearchMode);setActive(0);inputRef.current?.focus();setOpen(false);}}><option value="ask">Ask</option><option value="records">Records</option><option value="commands">Commands</option></select><Icon name="chevron" size={11}/></span>}
    <Icon name="search" size={15}/>
    {mode!=='commands'&&groupLabel&&<button className="search-group-chip" aria-label={`Remove ${mode==='ask'?'graph ':''}group filter ${groupLabel}`} title={mode==='ask'?`Graph is filtered to ${groupLabel}. Questions search all sources. Click to clear the graph filter.`:`Remove ${groupLabel} filter`} onClick={onClearGroup}><span>{mode==='ask'?'Graph: ':''}{groupLabel}</span><Icon name="close" size={11}/></button>}
    <input ref={inputRef} type="text" role="combobox" aria-label={mode==='ask'?'Ask your library':mode==='records'?'Find records':'Find commands and groups'} aria-expanded={open} aria-controls={`${id}-list`} aria-autocomplete="list" aria-activedescendant={open&&selected>=0?`${id}-${selected}`:undefined} autoComplete="off" placeholder={placeholder} value={query} onFocus={()=>{setActive(0);setOpen(true);}} onPointerDown={()=>setOpen(true)} onChange={event=>{onQuery(event.target.value);setActive(0);setOpen(true);}} onKeyDown={event=>{
      if(event.nativeEvent.isComposing)return;
      if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){setActive(0);setOpen(true);return;}
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();setOpen(false);}
      else if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();setOpen(true);setActive(nextSearchIndex(selected,event.key==='ArrowDown'?1:-1,rows.length));}
      else if(event.key==='Enter'){event.preventDefault();choose(open?selected:0);}
    }}/>
    {query&&<button className="search-clear" aria-label="Clear search" onClick={()=>{onQuery('');setActive(0);inputRef.current?.focus();setOpen(false);}}><Icon name="close" size={13}/></button>}
    <kbd className="search-shortcut" aria-hidden="true">⌘ K</kbd>
    {open&&<div className="unified-search-popover"><div id={`${id}-list`} role="listbox" aria-label="Search suggestions">{rows.map((row,index)=><div key={row.id}>
      {(index===0||row.section!==rows[index-1].section)&&<div className="unified-search-section" aria-hidden="true">{row.section==='Groups'?'Groups':row.section||'Commands'}</div>}
      <div id={`${id}-${index}`} role="option" aria-selected={index===selected} className={`unified-search-option ${index===selected?'selected':''}`} onMouseDown={event=>event.preventDefault()} onMouseEnter={()=>setActive(index)} onClick={()=>choose(index)}><span className="unified-search-icon" aria-hidden="true">{row.icon}</span><span className="unified-search-copy"><span>{row.label}</span>{row.description&&<small>{row.description}</small>}</span>{index===selected&&<span aria-hidden="true">↵</span>}</div>
    </div>)}</div>{!rows.length&&<p className="unified-search-empty" role="status">No matching commands. Switch to Ask or Records to search your library.</p>}<div className="unified-search-footer">↑ ↓ to move · Enter to select · Esc to close</div></div>}
  </div>;
}
