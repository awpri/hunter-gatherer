import {describe,it,expect} from 'vitest';
import {decideAgentFollow,FOLLOW_INPUT_COOLDOWN} from './agentFollow';
const focus={token:'epoch:7',recordIds:['memory:new','document:source'],kind:'write' as const,timestamp:10000,operation:'remember'};
const options={enabled:true,hidden:false,dialogOpen:false,now:11000,lastInput:0,visibleIds:new Set(['memory:new','document:source'])};
describe('polite agent camera',()=>{
  it('frames only returned records visible in the current graph',()=>{
    expect(decideAgentFollow(focus,{...options,visibleIds:new Set(['document:source'])})).toEqual({action:'focus',request:{token:'epoch:7',ids:['document:source'],kind:'write'}});
  });
  it('yields to navigation, dialogs, hidden tabs, and the follow toggle',()=>{
    for(const change of [{lastInput:11000},{lastInput:11000-FOLLOW_INPUT_COOLDOWN+1},{enabled:false},{hidden:true},{dialogOpen:true}])expect(decideAgentFollow(focus,{...options,...change}).action).toBe('ignore');
  });
  it('never replays an event that happened before a manual gesture even after cooldown',()=>{
    expect(decideAgentFollow({...focus,timestamp:12500},{...options,now:20000,lastInput:13000}).action).toBe('ignore');
    expect(decideAgentFollow({...focus,timestamp:14000},{...options,now:20000,lastInput:13000}).action).toBe('focus');
    expect(decideAgentFollow({...focus,timestamp:NaN},options).action).toBe('ignore');
  });
  it('waits briefly for inserted records then expires instead of following stale events',()=>{
    expect(decideAgentFollow(focus,{...options,visibleIds:new Set()}).action).toBe('wait');
    expect(decideAgentFollow(focus,{...options,now:19000,visibleIds:new Set()}).action).toBe('ignore');
    expect(decideAgentFollow({...focus,timestamp:30000},options).action).toBe('ignore');
  });
});
