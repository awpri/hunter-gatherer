export type ActivityPhase = 'start' | 'complete' | 'error';
export type ActivityKind = 'read' | 'write';
export interface ActivityEvent {
  seq:number; operation_id:string; phase:ActivityPhase; operation:string; origin:'agent'|'library';
  record_ids:string[]; links:{source:string;target:string}[]; changed:boolean; timestamp:number;
}
export interface ActivityPacket {epoch:string;cursor:number;events:ActivityEvent[]}
export interface GraphActivity {
  nodes:Record<string,{kind:ActivityKind;token:number}>;
  links:{source:string;target:string;kind:ActivityKind;token:number}[];
  focus?:AgentFocus;
}
/** Only actual completed agent operations may steer the optional map camera. */
export interface AgentFocus {token:string;recordIds:string[];kind:ActivityKind;timestamp:number;operation:string}
export interface ActivityStatus {label:string;origin:'agent'|'library';phase:ActivityPhase}
type TimedEvent = {event:ActivityEvent;expires:number};
export interface ActivityState {epoch:string|null;cursor:number|undefined;recent:TimedEvent[];running:TimedEvent[];changedOperations:string[]}
export const ACTIVITY_DURATION = 2800;
export const emptyActivity = ():GraphActivity => ({nodes:{},links:[]});
export const initialActivity = ():ActivityState => ({epoch:null,cursor:undefined,recent:[],running:[],changedOperations:[]});
const recordId = (id:string) => typeof id==='string' && /^(memory|document):[^\s]+$/.test(id);
export function operationKind(operation:string, changed=false, phase:ActivityPhase='complete'):ActivityKind {
  if(phase!=='start')return changed?'write':'read';
  return changed || /(?:remember|replace|forget|set_core|pin|ingest|upload|review_import|propose_import|add_memory|create|update)/.test(operation) ? 'write' : 'read';
}
export function operationLabel(operation:string):string {
  if(/remember|add_memory|create/.test(operation))return 'Adding';
  if(/ingest|upload/.test(operation))return 'Importing';
  if(/replace|update|pin|set_core/.test(operation))return 'Updating';
  if(/forget/.test(operation))return 'Removing';
  if(/review|propose/.test(operation))return 'Reviewing';
  if(/health|check/.test(operation))return 'Checking';
  return 'Reading';
}
/** Cursor deduplication is independent of visible records and handles events
 * arriving in bursts. New process epochs discard all previous transient state. */
export function acceptActivity(previous:ActivityState,packet:ActivityPacket,now:number):{state:ActivityState;changed:string[]} {
  const reset=previous.epoch!==packet.epoch;
  const base=reset?initialActivity():previous;
  const floor=base.cursor ?? -1;
  const seen=new Set(base.changedOperations);
  const changed:string[]=[];
  const events=[...packet.events].sort((a,b)=>a.seq-b.seq);
  const accepted:TimedEvent[]=[];let last=floor;
  const retired=new Set<string>();
  const running=new Map(base.running.filter(item=>item.expires>now).map(item=>[item.event.operation_id,item]));
  for(const event of events){
    if(!Number.isFinite(event.seq)||event.seq<=last)continue;
    last=event.seq;
    if(event.phase!=='start'){
      retired.add(event.operation_id);
      for(let i=accepted.length-1;i>=0;i--)if(accepted[i].event.operation_id===event.operation_id)accepted.splice(i,1);
    }
    const expires=Math.min(now+ACTIVITY_DURATION,event.timestamp*1000+ACTIVITY_DURATION);
    if(expires>now)accepted.push({event,expires});
    if(event.phase==='start'){const end=Math.min(now+120000,event.timestamp*1000+120000);if(end>now)running.set(event.operation_id,{event,expires:end});}else running.delete(event.operation_id);
    if(event.phase==='complete'&&event.changed&&!seen.has(event.operation_id)){
      seen.add(event.operation_id);changed.push(event.operation_id);
    }
  }
  return {state:{epoch:packet.epoch,cursor:Math.max(last,packet.cursor),recent:[...base.recent.filter(item=>item.expires>now&&!retired.has(item.event.operation_id)),...accepted].slice(-80),running:[...running.values()].slice(-32),changedOperations:[...seen].slice(-512)},changed};
}
export function pruneActivity(state:ActivityState,now:number):ActivityState {
  const recent=state.recent.filter(item=>item.expires>now);
  const running=state.running.filter(item=>item.expires>now);
  return recent.length===state.recent.length&&running.length===state.running.length?state:{...state,recent,running};
}
export function activitySnapshot(state:ActivityState,now:number):{activity:GraphActivity;status:ActivityStatus|null} {
  const activity=emptyActivity();const active=state.recent.filter(item=>item.expires>now);
  const running=state.running.filter(item=>item.expires>now);
  const latest=running[running.length-1]?.event || active[active.length-1]?.event;
  for(const {event} of active){
    if(event.phase==='error')continue;
    const kind=operationKind(event.operation,event.changed,event.phase);
    for(const id of event.record_ids.filter(recordId).slice(-128))activity.nodes[id]={kind,token:event.seq};
    for(const link of event.links.filter(link=>recordId(link.source)&&recordId(link.target)&&link.source!==link.target)) {
      const key=`${link.source}\0${link.target}`;
      const index=activity.links.findIndex(item=>`${item.source}\0${item.target}`===key);
      const item={...link,kind,token:event.seq};
      if(index>=0)activity.links[index]=item;else activity.links.push(item);
    }
  }
  const nodeEntries=Object.entries(activity.nodes).slice(-128);activity.nodes=Object.fromEntries(nodeEntries);
  activity.links=activity.links.slice(-128);
  const agentEvent=[...active].reverse().find(({event})=>event.origin==='agent'&&event.phase==='complete'&&event.record_ids.some(recordId))?.event;
  if(agentEvent)activity.focus={token:`${state.epoch}:${agentEvent.seq}`,recordIds:[...new Set(agentEvent.record_ids.filter(recordId))].slice(0,24),kind:operationKind(agentEvent.operation,agentEvent.changed,agentEvent.phase),timestamp:agentEvent.timestamp*1000,operation:agentEvent.operation};
  return {activity,status:latest?{label:latest.phase==='error'?'Operation failed':operationLabel(latest.operation),origin:latest.origin,phase:latest.phase}:null};
}
/** A backend activity link is drawn only when both endpoints are currently
 * visible and the graph contains the corresponding actual citation edge. */
export function visibleSourceActivity(activity:GraphActivity,visibleIds:ReadonlySet<string>,sourceEdges:readonly {source:string;target:string;kind:string}[]):GraphActivity {
  const sourcePairs=new Set(sourceEdges.filter(edge=>edge.kind==='source').flatMap(edge=>[`${edge.source}\0${edge.target}`,`${edge.target}\0${edge.source}`]));
  const focusIds=activity.focus?.recordIds.filter(id=>visibleIds.has(id));
  return {nodes:Object.fromEntries(Object.entries(activity.nodes).filter(([id])=>visibleIds.has(id))),
    links:activity.links.filter(link=>visibleIds.has(link.source)&&visibleIds.has(link.target)&&sourcePairs.has(`${link.source}\0${link.target}`)),
    ...(activity.focus&&focusIds?.length?{focus:{...activity.focus,recordIds:focusIds}}:{})};
}
