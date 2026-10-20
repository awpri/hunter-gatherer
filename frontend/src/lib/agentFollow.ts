import type {AgentFocus} from './activity';

export const FOLLOW_INPUT_COOLDOWN=6500;
export const FOLLOW_EVENT_MAX_AGE=8000;
export type FocusRequest={token:string;ids:string[];kind:'read'|'write'};
export type FollowDecision={action:'ignore'|'wait'|'focus';request?:FocusRequest};

/** Never replay activity after a user gesture, dialog, tab switch, or toggle. */
export function decideAgentFollow(focus:AgentFocus,options:{enabled:boolean;hidden:boolean;dialogOpen:boolean;now:number;lastInput:number;visibleIds:ReadonlySet<string>}):FollowDecision{
  if(!Number.isFinite(focus.timestamp)||focus.timestamp<=options.lastInput||!options.enabled||options.hidden||options.dialogOpen||options.now-options.lastInput<FOLLOW_INPUT_COOLDOWN||options.now-focus.timestamp>FOLLOW_EVENT_MAX_AGE||focus.timestamp>options.now+1000)return {action:'ignore'};
  const ids=focus.recordIds.filter(id=>options.visibleIds.has(id));
  // A just-created record may arrive one graph refresh after its activity event.
  if(!ids.length)return {action:'wait'};
  return {action:'focus',request:{token:focus.token,ids,kind:focus.kind}};
}
