import {describe,it,expect} from 'vitest';
import {ACTIVITY_DURATION,acceptActivity,activitySnapshot,initialActivity,pruneActivity,visibleSourceActivity,type ActivityEvent} from './activity';
const now=1000000;
const event=(seq:number,options:Partial<ActivityEvent>={}):ActivityEvent=>({seq,operation_id:`op-${seq}`,phase:'complete',operation:'get_memory',origin:'agent',record_ids:['memory:a'],links:[],changed:false,timestamp:now/1000,...options});
describe('live library activity',()=>{
  it('bootstraps without replay and deduplicates repeated sequences and changed operations',()=>{
    let result=acceptActivity(initialActivity(),{epoch:'one',cursor:10,events:[]},now);
    expect(activitySnapshot(result.state,now).status).toBeNull();
    result=acceptActivity(result.state,{epoch:'one',cursor:13,events:[event(11,{operation_id:'write',changed:true}),event(11,{operation_id:'write',changed:true}),event(12,{operation_id:'write',changed:true}),event(13)]},now);
    expect(result.changed).toEqual(['write']);
    expect(result.state.recent).toHaveLength(2);
    const duplicate=acceptActivity(result.state,{epoch:'one',cursor:13,events:[event(11,{changed:true}),event(12,{changed:true})]},now);
    expect(duplicate.changed).toEqual([]);
    expect(duplicate.state.recent).toHaveLength(2);
  });
  it('clears pending nodes and cursors across privacy reset or backend restart',()=>{
    const previous=acceptActivity(initialActivity(),{epoch:'old',cursor:50,events:[event(50,{record_ids:['memory:private']})]},now).state;
    expect(activitySnapshot(initialActivity(),now).activity.nodes).toEqual({});
    const restart=acceptActivity(previous,{epoch:'new',cursor:1,events:[event(1,{record_ids:['memory:public']})]},now).state;
    expect(restart.cursor).toBe(1);
    expect(activitySnapshot(restart,now).activity.nodes).toEqual({'memory:public':{kind:'read',token:1}});
  });
  it('keeps overlapping starts active until matching completion, with a bounded timeout',()=>{
    let state=acceptActivity(initialActivity(),{epoch:'one',cursor:2,events:[event(1,{phase:'start',operation_id:'first'}),event(2,{phase:'start',operation_id:'second'})]},now).state;
    expect(activitySnapshot(state,now+10000).status?.phase).toBe('start');
    expect(activitySnapshot(state,now+10000).activity.nodes).toEqual({});
    state=acceptActivity(state,{epoch:'one',cursor:3,events:[event(3,{operation_id:'second'})]},now+10000).state;
    expect(activitySnapshot(state,now+10000).status?.phase).toBe('start');
    state=acceptActivity(state,{epoch:'one',cursor:4,events:[event(4,{operation_id:'first',timestamp:(now+11000)/1000})]},now+11000).state;
    expect(activitySnapshot(state,now+11000).status?.phase).toBe('complete');
    expect(activitySnapshot(pruneActivity(state,now+121000),now+121000).status).toBeNull();
  });
  it('processes old changed completions without visually replaying them after tab resume',()=>{
    const result=acceptActivity(initialActivity(),{epoch:'one',cursor:1,events:[event(1,{changed:true,timestamp:(now-60000)/1000})]},now);
    expect(result.changed).toEqual(['op-1']);
    expect(activitySnapshot(result.state,now)).toEqual({activity:{nodes:{},links:[]},status:null});
  });
  it('never shows a completed no-op as a mutation or leaves its start highlight behind',()=>{
    for(const operation of ['remember','ingest_document','set_core_memory','replace_memory']){
      const start=event(1,{phase:'start',operation_id:'noop',operation});
      let state=acceptActivity(initialActivity(),{epoch:'one',cursor:1,events:[start]},now).state;
      expect(activitySnapshot(state,now).activity.nodes['memory:a'].kind).toBe('write');
      const noop=event(2,{operation_id:'noop',operation,changed:false});
      const result=acceptActivity(state,{epoch:'one',cursor:2,events:[noop]},now+100);
      expect(result.changed).toEqual([]);
      expect(activitySnapshot(result.state,now+100).activity.nodes['memory:a'].kind).toBe('read');
      const noResult=acceptActivity(state,{epoch:'one',cursor:2,events:[{...noop,record_ids:[]}]},now+100);
      expect(activitySnapshot(noResult.state,now+100).activity.nodes).toEqual({});
      const mutation=acceptActivity(state,{epoch:'one',cursor:2,events:[{...noop,changed:true}]},now+100);
      expect(activitySnapshot(mutation.state,now+100).activity.nodes['memory:a'].kind).toBe('write');
    }
  });
  it('bounds burst storage and expires every completed animation',()=>{
    const events=Array.from({length:500},(_,i)=>event(i+1,{changed:true,record_ids:[`memory:${i}`]}));
    const result=acceptActivity(initialActivity(),{epoch:'one',cursor:500,events},now);
    expect(result.changed).toHaveLength(500);
    expect(result.state.recent).toHaveLength(80);
    expect(result.state.changedOperations.length).toBeLessThanOrEqual(512);
    expect(Object.keys(activitySnapshot(result.state,now).activity.nodes).length).toBeLessThanOrEqual(128);
    expect(activitySnapshot(pruneActivity(result.state,now+ACTIVITY_DURATION+1),now+ACTIVITY_DURATION+1).activity.nodes).toEqual({});
  });
  it('renders only known nodes and backend-reported links which are actual source edges',()=>{
    const state=acceptActivity(initialActivity(),{epoch:'one',cursor:1,events:[event(1,{record_ids:['memory:a','document:b','memory:unknown','topic:tag'],links:[{source:'memory:a',target:'document:b'},{source:'memory:a',target:'memory:c'},{source:'memory:a',target:'document:unknown'}]})]},now).state;
    const active=activitySnapshot(state,now).activity;
    expect(active.nodes['topic:tag']).toBeUndefined();
    const visible=visibleSourceActivity(active,new Set(['memory:a','document:b','memory:c']),[{source:'memory:a',target:'document:b',kind:'source'},{source:'memory:a',target:'memory:c',kind:'topic'}]);
    expect(Object.keys(visible.nodes)).toEqual(['memory:a','document:b']);
    expect(visible.links).toEqual([{source:'memory:a',target:'document:b',kind:'read',token:1}]);
    expect(visible.focus?.recordIds).toEqual(['memory:a','document:b']);
  });
  it('follows completed agent work, never a library click, failure or operation start',()=>{
    for(const override of [{origin:'library' as const},{phase:'error' as const},{phase:'start' as const}]){
      const state=acceptActivity(initialActivity(),{epoch:'one',cursor:1,events:[event(1,override)]},now).state;
      expect(activitySnapshot(state,now).activity.focus).toBeUndefined();
    }
    const state=acceptActivity(initialActivity(),{epoch:'one',cursor:2,events:[event(1,{changed:true}),event(2,{origin:'library',record_ids:['memory:other']})]},now).state;
    expect(activitySnapshot(state,now).activity.focus).toEqual({token:'one:1',recordIds:['memory:a'],kind:'write',timestamp:now,operation:'get_memory'});
  });
});
