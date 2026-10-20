import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import type {AgentFocus} from './activity';
import {decideAgentFollow,FOLLOW_EVENT_MAX_AGE,type FocusRequest} from './agentFollow';

/** A polite, optional camera cue; the renderer still owns camera geometry. */
export function useAgentFollow(focus:AgentFocus|undefined,enabled:boolean,recordIds:readonly string[]){
  const [request,setRequest]=useState<FocusRequest>();
  const inputAt=useRef(-Infinity),seen=useRef(new Set<string>()),pending=useRef<AgentFocus>();
  const visibleIds=useMemo(()=>new Set(recordIds),[recordIds.join('|')]);
  const markInteraction=useCallback(()=>{inputAt.current=Date.now();if(pending.current)seen.current.add(pending.current.token);pending.current=undefined;setRequest(undefined);},[]);

  useEffect(()=>{
    const input=()=>markInteraction();
    const visibility=()=>markInteraction();
    window.addEventListener('pointerdown',input,{capture:true,passive:true});
    window.addEventListener('wheel',input,{capture:true,passive:true});
    window.addEventListener('keydown',input,true);
    document.addEventListener('visibilitychange',visibility);
    return()=>{window.removeEventListener('pointerdown',input,true);window.removeEventListener('wheel',input,true);window.removeEventListener('keydown',input,true);document.removeEventListener('visibilitychange',visibility);};
  },[markInteraction]);

  useEffect(()=>{
    if(focus&&!seen.current.has(focus.token))pending.current=focus;
    const next=pending.current;
    if(!next)return;
    const evaluate=()=>{
      const decision=decideAgentFollow(next,{enabled,hidden:document.hidden,dialogOpen:!!document.querySelector('[role="dialog"]'),now:Date.now(),lastInput:inputAt.current,visibleIds});
      if(decision.action==='wait')return;
      seen.current.add(next.token);pending.current=undefined;
      if(seen.current.size>100)seen.current=new Set([...seen.current].slice(-60));
      if(decision.action==='focus')setRequest(decision.request);
    };
    evaluate();
    if(!pending.current)return;
    const timer=setTimeout(evaluate,Math.max(1,FOLLOW_EVENT_MAX_AGE-(Date.now()-next.timestamp)+1));
    return()=>clearTimeout(timer);
  },[focus,enabled,visibleIds]);

  useEffect(()=>{if(!enabled){setRequest(undefined);if(pending.current)seen.current.add(pending.current.token);pending.current=undefined;}},[enabled]);
  return {focusRequest:request,onUserNavigate:markInteraction};
}
