import {useState, type CSSProperties} from 'react';
import {Icon} from '../views/LibraryShared';
import type {LibraryGroups, LibraryGroup} from '../lib/libraryApi';
import {VisualIcon} from './VisualPicker';
import {visualImage} from '../lib/boardApi';
import './LibraryGroupsNav.css';

export const GROUP_COLORS=['#7fa9e5','#79c3ac','#bc98de','#dcb278','#e498a4','#91bfdf','#afbf82','#a799d5'];
const ICON_ALIASES:Record<string,string>={user:'personal','graduation-cap':'education',folder:'project','check-circle':'decision',sliders:'preferences',wrench:'workflow','file-text':'source',hash:'topics',inbox:'archive',briefcase:'work','book-open':'knowledge'};
export const groupIcon=(icon?:string)=>ICON_ALIASES[icon||'']||icon||'topics';
export function sectionsOf(groups:LibraryGroups|null){
  if(!groups)return [];
  if(groups.sections?.length)return groups.sections.map(section=>({...section,groups:groups.groups.filter(group=>group.section_id===section.id)}));
  return [{id:'section:topics',label:'Groups',icon:'topics',count:groups.total,groups:groups.groups}];
}
export function GroupOptionList({groups}:{groups:LibraryGroups|null}){
  return <>{sectionsOf(groups).map(section=><optgroup key={section.id} label={section.label}>{section.groups.map(group=><option key={group.id} value={group.id}>{group.label} ({group.count})</option>)}</optgroup>)}</>;
}
export function LibraryGroupsNav({groups,active,pinned,choose}:{groups:LibraryGroups|null;active:string;pinned:boolean;choose:(id:string)=>void}){
  const [closed,setClosed]=useState<Set<string>>(new Set());
  function toggle(id:string){setClosed(previous=>{const next=new Set(previous);if(next.has(id))next.delete(id);else next.add(id);return next;});}
  function groupRow(group:LibraryGroup,index:number,collapsed:boolean){
    const image = visualImage((group as LibraryGroup & {image?: string}).image);
    return <button key={group.id} tabIndex={collapsed?-1:0} className={`group-item ${active===group.id?'selected':''}`} style={{'--group-color':GROUP_COLORS[group.color_index%GROUP_COLORS.length],'--row-index':index} as CSSProperties} title={`${group.memory_count} memories · ${group.document_count} files`} onClick={()=>choose(active===group.id?'':group.id)}><span className="group-subject-icon"><>{image ? <img src={image} alt=""/> : <VisualIcon icon={groupIcon(group.icon)} size={16}/>}</></span><span>{group.label}</span><small>{group.count}</small></button>;
  }
  return <div className="group-list sectioned-groups">
    <button className={`group-item all-records ${!active&&!pinned?'selected':''}`} onClick={()=>choose('')}><Icon name="grid" size={15}/><span>Everything</span><small>{groups?.total??'—'}</small></button>
    {sectionsOf(groups).map(section=>{const collapsed=closed.has(section.id);const id=section.id.replaceAll(':','-');return <section className={`sidebar-section ${collapsed?'collapsed':''}`} key={section.id}>
      <button className="sidebar-section-toggle" aria-expanded={!collapsed} aria-controls={id} onClick={()=>toggle(section.id)}><Icon name={groupIcon(section.icon)} size={12}/><span>{section.label}</span><Icon name="chevron" size={12}/></button>
      <div id={id} className="sidebar-section-body" aria-hidden={collapsed}><div>{section.groups.map((group,index)=>groupRow(group,index,collapsed))}</div></div>
    </section>;})}
    {!groups&&<span className="sidebar-loading">Loading…</span>}
  </div>;
}
