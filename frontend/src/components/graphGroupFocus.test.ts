import {describe, expect, it} from 'vitest';
import {groupAtPoint, groupLensAmount} from './graphGroupFocus';
import {magneticFocusPositions} from './graphMagneticFocus';
import type {GroupBounds} from './graphLayout';
const a: GroupBounds={id:'a',x:0,y:0,width:500,height:700,headerHeight:80,recordScale:1,contentPadding:20,colorIndex:0,nodeIds:['a1','a2'],minimumSize:{width:1,height:1}};
const b: GroupBounds={...a,id:'b',x:520,nodeIds:['b1','b2']};
describe('group-local cursor lens',()=>{
  it('uses fixed box ownership, including empty/header space and gaps',()=>{
    expect(groupAtPoint([a,b],{x:80,y:20})?.id).toBe('a');
    expect(groupAtPoint([a,b],{x:720,y:680})?.id).toBe('b');
    expect(groupAtPoint([a,b],{x:510,y:300})).toBeUndefined();
    expect(groupAtPoint([a,b],null)).toBeUndefined();
  });
  it('eases out before crossing a group boundary',()=>{
    expect(groupLensAmount(a,{x:500,y:300},1)).toBe(0);
    expect(groupLensAmount(a,{x:488,y:300},1)).toBeCloseTo(.5);
    expect(groupLensAmount(a,{x:470,y:300},1)).toBe(1);
    expect(groupLensAmount(undefined,{x:510,y:300},1)).toBe(0);
  });
  it('cannot displace another group even when the lens radius reaches it',()=>{
    const points=new Map([['a1',{x:200,y:300}],['a2',{x:310,y:300}],['b1',{x:700,y:300}],['b2',{x:810,y:300}]]);
    const chosen=groupAtPoint([a,b],{x:400,y:300})!;
    const result=magneticFocusPositions(points,[chosen],{x:400,y:300},1,{radius:1000,cardGap:228,cardHeight:214});
    expect(result.get('b1')).toEqual(points.get('b1'));expect(result.get('b2')).toEqual(points.get('b2'));
    expect(result.get('a1')).not.toEqual(points.get('a1'));
  });
});
