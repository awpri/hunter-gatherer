import {useEffect,useRef,useState} from 'react';
import {libraryApi,isAbort} from './libraryApi';
import {acceptActivity,activitySnapshot,emptyActivity,initialActivity,pruneActivity,type ActivityPacket,type ActivityStatus,type GraphActivity} from './activity';
interface ActivityView {activity:GraphActivity;status:ActivityStatus|null;connected:boolean;scope:boolean}
export function useLibraryActivity(includePrivate:boolean,onChanged:()=>void):{activity:GraphActivity;status:ActivityStatus|null;connected:boolean} {
  const changed=useRef(onChanged);changed.current=onChanged;
  const [view,setView]=useState<ActivityView>(()=>({activity:emptyActivity(),status:null,connected:false,scope:includePrivate}));
  useEffect(()=>{
    let live=true;let state=initialActivity();let connected=false;let pending=false;
    let pollTimer:ReturnType<typeof setTimeout>|undefined;let expiryTimer:ReturnType<typeof setTimeout>|undefined;
    const publish=()=>{if(!live)return;const snapshot=activitySnapshot(state,Date.now());setView({...snapshot,connected,scope:includePrivate});};
    const scheduleExpiry=()=>{
      if(expiryTimer)clearTimeout(expiryTimer);
      const expires=Math.min(...state.recent.map(item=>item.expires),...state.running.map(item=>item.expires));
      if(Number.isFinite(expires))expiryTimer=setTimeout(()=>{state=pruneActivity(state,Date.now());publish();scheduleExpiry();},Math.max(10,expires-Date.now()+10));
    };
    const poll=async()=>{
      if(!live||document.hidden||pending)return;
      pending=true;
      try{
        const packet=await libraryApi.request<ActivityPacket>('activity',{after:state.cursor,include_sensitive:includePrivate});
        if(!live||document.hidden)return;
        const accepted=acceptActivity(state,packet,Date.now());state=accepted.state;connected=true;
        publish();scheduleExpiry();
        for(const operation of accepted.changed){if(!live)break;void operation;changed.current();}
      }catch(error){if(live&&!isAbort(error)){connected=false;state={...state,recent:[],running:[]};publish();}}
      finally{pending=false;if(live&&!document.hidden)pollTimer=setTimeout(poll,1000);}
    };
    const visibility=()=>{
      if(pollTimer)clearTimeout(pollTimer);
      if(document.hidden){state={...state,recent:[]};publish();}
      else if(!pending)void poll();
    };
    setView({activity:emptyActivity(),status:null,connected:false,scope:includePrivate});
    document.addEventListener('visibilitychange',visibility);void poll();
    return()=>{live=false;if(pollTimer)clearTimeout(pollTimer);if(expiryTimer)clearTimeout(expiryTimer);document.removeEventListener('visibilitychange',visibility);};
  },[includePrivate]);
  return view.scope===includePrivate?view:{activity:emptyActivity(),status:null,connected:false};
}
