import {describe, expect, it} from 'vitest';
import {layoutOverview, type LayoutInputNode} from './graphLayout';
import {organicOverviewPositions} from './graphConstellation';

describe('organic overview positions', () => {
  for (const counts of [[41,19,13,11,7,4], [4,5,3,6,9,1,14,11,10,13,1,4,1,1,2,1,2,7], [200]]) {
    it(`bounds and separates ${counts.reduce((a,b)=>a+b,0)} records in ${counts.length} groups`, () => {
      const groups = counts.map((_,i)=>({id:`group:${i}`}));
      const nodes: LayoutInputNode[] = groups.flatMap((g,i)=>Array.from({length:counts[i]},(_,j)=>({id:`${g.id}:record:${j}`,type:'document',group_id:g.id})));
      for (const size of [{width:1190,height:650},{width:1056,height:576}]) {
        const layout = layoutOverview(nodes,[],groups,size);
        const points = organicOverviewPositions(layout);
        expect(points.size).toBe(nodes.length);
        expect([...points]).toEqual([...organicOverviewPositions(layoutOverview([...nodes].reverse(),[],[...groups].reverse(),size))]);
        for (const group of layout.groups) {
          const positions = group.nodeIds.map(id=>points.get(id)!);
          for (const point of positions) {
            expect(point.x).toBeGreaterThanOrEqual(group.x+group.contentPadding+95.99*group.recordScale);
            expect(point.x).toBeLessThanOrEqual(group.x+group.width-group.contentPadding-95.99*group.recordScale);
            expect(point.y).toBeGreaterThanOrEqual(group.y+group.headerHeight+95.99*group.recordScale);
            expect(point.y).toBeLessThanOrEqual(group.y+group.height-group.contentPadding-95.99*group.recordScale);
          }
          for (let i=0;i<positions.length;i++) for (let j=i+1;j<positions.length;j++) {
            expect(Math.hypot(positions[i].x-positions[j].x,positions[i].y-positions[j].y)).toBeGreaterThan(187*group.recordScale);
            expect(Math.abs(positions[i].x-positions[j].x) >= 219*group.recordScale || Math.abs(positions[i].y-positions[j].y) >= 209*group.recordScale).toBe(true);
          }
        }
      }
    });
  }

  for (const count of [2, 3, 4, 17, 41]) for (const dimensions of [{width:2600,height:1200},{width:1100,height:3800},{width:2100,height:2400}]) {
    it(`fills a resized ${dimensions.width} × ${dimensions.height} body with ${count} records`, () => {
      const nodes: LayoutInputNode[] = Array.from({length:count},(_,i)=>({id:`node:${i}`,type:'document',group_id:'A'}));
      const layout = layoutOverview(nodes,[],[{id:'A'}],{width:1190,height:650},{groupSizes:{A:dimensions}});
      const group=layout.groups[0], points=[...organicOverviewPositions(layout).values()];
      const margin=96*group.recordScale;
      const left=group.x+group.contentPadding+margin, right=group.x+group.width-group.contentPadding-margin;
      const top=group.y+group.headerHeight+margin, bottom=group.y+group.height-group.contentPadding-margin;
      expect(Math.min(...points.map(p=>p.x))).toBeCloseTo(left);
      expect(Math.max(...points.map(p=>p.x))).toBeCloseTo(right);
      expect(Math.min(...points.map(p=>p.y))).toBeCloseTo(top);
      expect(Math.max(...points.map(p=>p.y))).toBeCloseTo(bottom);
      for(let i=0;i<points.length;i++) for(let j=i+1;j<points.length;j++) {
        expect(Math.hypot(points[i].x-points[j].x,points[i].y-points[j].y)).toBeGreaterThan(187*group.recordScale);
        expect(Math.abs(points[i].x-points[j].x) >= 219*group.recordScale || Math.abs(points[i].y-points[j].y) >= 209*group.recordScale).toBe(true);
      }
    });
  }
  it('centers isolated records and makes triples triangular',()=>{
    const nodes: LayoutInputNode[] = Array.from({length:3},(_,i)=>({id:`node:${i}`,type:'memory',group_id:'A'}));
    const layout=layoutOverview(nodes,[],[],{width:1190,height:650});
    const p=[...organicOverviewPositions(layout).values()];
    expect(Math.abs((p[1].x-p[0].x)*(p[2].y-p[0].y)-(p[2].x-p[0].x)*(p[1].y-p[0].y))).toBeGreaterThan(100);
    const single=layoutOverview(nodes.slice(0,1),[],[],{width:1190,height:650});
    const point=organicOverviewPositions(single).get('node:0')!;
    expect(point.x).toBeCloseTo(single.groups[0].x+single.groups[0].width/2);
  });
});
