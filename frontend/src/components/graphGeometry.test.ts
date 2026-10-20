import {describe,it,expect} from 'vitest';
import {focusRecordBody,groupTypography,sourceCurve,neighborhood,viewportBounds} from './graphGeometry';
describe('graph geometry',()=>{
  it('preserves the curve control point in either activity direction',()=>{
    expect(sourceCurve({x:0,y:0},{x:100,y:0})).toBe('M 0 0 Q 50 9 100 0');
    expect(sourceCurve({x:100,y:0},{x:0,y:0})).toBe('M 100 0 Q 50 9 0 0');
    expect(sourceCurve({x:1,y:2},{x:1,y:2})).toBe('M 1 2 L 1 2');
  });
  it('keeps selection and direct neighbors without unrelated records',()=>{
    expect([...neighborhood('a',[{source:'a',target:'b'},{source:'c',target:'a'},{source:'b',target:'d'}])]).toEqual(['a','b','c']);
    expect([...neighborhood('isolated',[])]).toEqual(['isolated']);
    expect(neighborhood(null,[]).size).toBe(0);
  });
  it('projects the camera into graph coordinates',()=>{
    expect(viewportBounds({x:-100,y:50,scale:2},{width:800,height:600})).toEqual({x:50,y:-25,width:400,height:300});
  });
});

import {semanticLayers,zoomAt,driftPoint} from './graphGeometry';
describe('semantic canvas',()=>{
 it('adds readable details while retaining the permanent overview summary',()=>{
  for(let scale=.01;scale<5;scale+=.013){const a=semanticLayers(scale,.35);expect(a.detail).toBeGreaterThanOrEqual(0);expect(a.detail).toBeLessThanOrEqual(1);expect(a.preview).toBe(1);if(a.records)expect(17*scale).toBeGreaterThanOrEqual(10);if(a.descriptions)expect(15*scale).toBeGreaterThanOrEqual(10);}
  expect(semanticLayers(.35,.35).records).toBe(false);expect(semanticLayers(.71,.35).descriptions).toBe(true);expect(semanticLayers(.71,.35).metadata).toBe(false);expect(semanticLayers(1.1,.35).metadata).toBe(true);expect(semanticLayers(1.2,.35).records).toBe(true);
 });
 it('keeps a pointer anchored through zoom and clamps the camera',()=>{
  const old={x:30,y:-20,scale:.4},pointer={x:217,y:311},next=zoomAt(old,2,pointer);expect((pointer.x-next.x)/next.scale).toBeCloseTo((pointer.x-old.x)/old.scale);expect((pointer.y-next.y)/next.scale).toBeCloseTo((pointer.y-old.y)/old.scale);expect(zoomAt(old,1000,pointer).scale).toBe(4);
 });
 it('keeps animation deterministic and bounded around shared geometry',()=>{
  const p={x:100,y:200};for(let t=0;t<100;t+=.5){const drift=driftPoint(p,'a',t,6);expect(Math.abs(drift.x-p.x)).toBeLessThanOrEqual(6);expect(Math.abs(drift.y-p.y)).toBeLessThanOrEqual(3.9);expect(drift).toEqual(driftPoint(p,'a',t,6));}
 });
});

import {revealPoint} from './graphGeometry';
describe('keyboard camera reveal',()=>{
 it('leaves visible focus stationary and recenters offscreen focus without changing zoom',()=>{
  const view={x:20,y:-10,scale:1.75},size={width:617,height:720};
  expect(revealPoint(view,size,{x:120,y:160})).toBe(view);
  const point={x:900,y:-80},next=revealPoint(view,size,point);
  expect(next.scale).toBe(view.scale);expect(point.x*next.scale+next.x).toBe(size.width/2);expect(point.y*next.scale+next.y).toBe(size.height/2);
 });
});

import {semanticPoint} from './graphGeometry';
describe('semantic constellation expansion',()=>{
 it('interpolates from a reserved mini lane into the body without leaving its region',()=>{
  const group={x:100,y:200,width:440,height:420},point={x:430,y:380};
  const mini=semanticPoint(point,group,0),expanded=semanticPoint(point,group,1),middle=semanticPoint(point,group,.5);
  expect(expanded).toEqual(point);expect(middle.x).toBeCloseTo((mini.x+expanded.x)/2);expect(middle.y).toBeCloseTo((mini.y+expanded.y)/2);
  for(let detail=0;detail<=1;detail+=.01){const p=semanticPoint(point,group,detail);expect(p.x).toBeGreaterThan(group.x);expect(p.x).toBeLessThan(group.x+group.width);expect(p.y).toBeGreaterThan(group.y+56);expect(p.y).toBeLessThan(group.y+group.height);}
 });
});

import {fitWorld} from './graphGeometry';
describe('whole-world fitting',()=>{
 it('keeps every world boundary within the usable viewport for very large and small graphs',()=>{
  for(const size of [{width:900,height:660},{width:320,height:500},{width:280,height:200}])for(const bounds of [{x:0,y:0,width:600,height:450},{x:0,y:0,width:12000,height:18000},{x:200,y:300,width:900,height:800}]){
   const view=fitWorld(bounds,size);const left=bounds.x*view.scale+view.x,right=(bounds.x+bounds.width)*view.scale+view.x,top=bounds.y*view.scale+view.y,bottom=(bounds.y+bounds.height)*view.scale+view.y;
   expect(left).toBeGreaterThanOrEqual(20);expect(right).toBeLessThanOrEqual(size.width-20);expect(top).toBeGreaterThanOrEqual(21);expect(bottom).toBeLessThanOrEqual(size.height-63);
  }
 });
 it('allows zooming out past a fit without overriding the pointer anchor',()=>{
  const view=fitWorld({x:0,y:0,width:12000,height:18000},{width:900,height:660}),point={x:100,y:100};
  const next=zoomAt(view,.1,point,view.scale*.4);expect(next.scale).toBeCloseTo(view.scale*.4);expect((point.x-next.x)/next.scale).toBeCloseTo((point.x-view.x)/view.scale);
 });
});


describe('overview to record focus', () => {
  it('fits complete description lines into the actual header at narrow widths', () => {
    for (const contentHeight of [180, 260, 480, 900]) for (const scale of [.03, .08, .17, .4, 1.5]) {
      const type = groupTypography(contentHeight + 120, 628, .035, scale, false, contentHeight);
      const required = type.meta * 1.62 + type.title * 1.2 + type.gap * 2 + type.copy * 2.8;
      expect(required).toBeLessThanOrEqual(contentHeight - 5.99);
    }
  });
  it('opens large record bodies at readable scale with the first rows in view', () => {
    const points = Array.from({length: 13}, (_, index) => ({x: 250 + index % 7 * 262, y: 1500 + Math.floor(index / 7) * 208}));
    const view = focusRecordBody(points, {width: 1190, height: 650})!;
    expect(view.scale).toBeGreaterThanOrEqual(.8);
    expect(semanticLayers(view.scale).records).toBe(true);
    const first = points[0];
    expect(first.x * view.scale + view.x).toBeGreaterThan(100);
    expect(first.y * view.scale + view.y).toBeGreaterThan(80);
    expect(first.y * view.scale + view.y).toBeLessThan(400);
    expect(focusRecordBody([], {width: 1190, height: 650})).toBeNull();
  });
  it('keeps projected header type monotonic and readable throughout zoom', () => {
    let title = 0, copy = 0;
    for (let scale = .08; scale <= 1.5; scale += .01) {
      const type = groupTypography(1300, 2860, .08, scale);
      expect(type.title * scale).toBeGreaterThanOrEqual(title - .0001);
      expect(type.copy * scale).toBeGreaterThanOrEqual(copy - .0001);
      title = type.title * scale; copy = type.copy * scale;
      expect(title).toBeGreaterThanOrEqual(14 - .0001);
      expect(copy).toBeGreaterThanOrEqual(11 - .0001);
    }
    expect(groupTypography(1300, 2860, .08, .8).title * .8).toBe(24);
    const narrow = groupTypography(345, 628, .08, .8, true);
    expect(narrow.copy).toBeLessThanOrEqual(628 / 26);
  });
});
