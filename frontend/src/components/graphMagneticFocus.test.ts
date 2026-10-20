import {describe, expect, it} from 'vitest';
import {magneticFocusPositions} from './graphMagneticFocus';
import type {GroupBounds} from './graphLayout';

const group: GroupBounds={id:'A',x:0,y:0,width:1600,height:1200,headerHeight:200,contentPadding:30,recordScale:1,colorIndex:0,nodeIds:['a','b','near','far','edge'],minimumSize:{width:540,height:400}};
const base=new Map([['a',{x:600,y:650}],['b',{x:680,y:650}],['near',{x:850,y:720}],['far',{x:1300,y:850}],['edge',{x:130,y:500}]]);
const focus={x:640,y:650},options={radius:400,maxDisplacement:50,cardGap:160};

describe('local magnetic focus',()=>{
  it('separates rectangular cards along a short axis with small bounded movement',()=>{
    const points=new Map([['a',{x:600,y:650}],['b',{x:780,y:760}]]);
    const result=magneticFocusPositions(points,[{...group,nodeIds:['a','b']}],{x:690,y:705},1,
      {radius:400,maxDisplacement:40,cardGap:230,cardHeight:164});
    const a=result.get('a')!,b=result.get('b')!;
    expect(Math.abs(a.x-b.x)>=216||Math.abs(a.y-b.y)>=148).toBe(true);
    for(const[id,p]of result){const origin=points.get(id)!;expect(Math.hypot(p.x-origin.x,p.y-origin.y)).toBeLessThanOrEqual(40);}
  });
  it('is reversible and keeps every distant record exactly stationary',()=>{
    expect([...magneticFocusPositions(base,[group],null,1,options)]).toEqual([...base]);
    expect([...magneticFocusPositions(base,[group],focus,0,options)]).toEqual([...base]);
    const result=magneticFocusPositions(base,[group],focus,1,options);
    expect(result.get('far')).toEqual(base.get('far'));
    expect(result.get('edge')).toEqual(base.get('edge'));
    expect([...base]).toEqual([['a',{x:600,y:650}],['b',{x:680,y:650}],['near',{x:850,y:720}],['far',{x:1300,y:850}],['edge',{x:130,y:500}]]);
  });
  it('gently separates local neighbors without exceeding the displacement budget',()=>{
    const result=magneticFocusPositions(base,[group],focus,1,options);
    expect(result.get('b')!.x-result.get('a')!.x).toBeGreaterThan(130);
    for(const [id,p]of result){const origin=base.get(id)!;expect(Math.hypot(p.x-origin.x,p.y-origin.y)).toBeLessThanOrEqual(50.00001);}
    const half=magneticFocusPositions(base,[group],focus,.5,options);
    expect(half.get('b')!.x-half.get('a')!.x).toBeLessThan(result.get('b')!.x-result.get('a')!.x);
  });
  it('respects the group body and is deterministic across group/member ordering',()=>{
    const focus={x:160,y:500};
    const result=magneticFocusPositions(base,[group],focus,1,{radius:500,cardGap:300});
    for(const p of result.values()){expect(p.x).toBeGreaterThanOrEqual(126);expect(p.x).toBeLessThanOrEqual(1474);expect(p.y).toBeGreaterThanOrEqual(296);expect(p.y).toBeLessThanOrEqual(1074);}
    const reverse=magneticFocusPositions(new Map([...base].reverse()),[{...group,nodeIds:[...group.nodeIds].reverse()}],focus,1,{radius:500,cardGap:300});
    for(const[id,p]of result)expect(reverse.get(id)).toEqual(p);
  });
  it('tapers continuously to zero at the edge and preserves non-group records',()=>{
    const points=new Map([['a',{x:600,y:650}],['ungrouped',{x:610,y:650}]]);
    const result=magneticFocusPositions(points,[group],{x:200.001,y:650},1,{radius:400});
    expect(Math.abs(result.get('a')!.x-600)).toBeLessThan(.0001);
    expect(result.get('ungrouped')).toEqual(points.get('ungrouped'));
    expect([...magneticFocusPositions(base,[group],focus,1,{radius:0})]).toEqual([...base]);
  });
});
