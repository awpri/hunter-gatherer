import type {GroupBounds, Point} from './graphLayout';

export type MagneticFocusOptions = {
  /** All dimensions are world units, independent of the camera. */
  radius: number;
  maxDisplacement?: number;
  /** Optional desired local center spacing. Resolution is bounded, best effort. */
  cardGap?: number;
  /** When supplied, cardGap is width and this is height for rectangle clearance. */
  cardHeight?: number;
};

/** A local, reversible lens over the static cloud. It never changes membership
 * or snaps records to slots. Outside the base-position radius, records are fixed. */
export function magneticFocusPositions(base: ReadonlyMap<string, Point>, groups: readonly GroupBounds[], focus: Point | null,
  amount: number, options: MagneticFocusOptions): Map<string, Point> {
  const result = new Map(base);
  const strength = Math.max(0, Math.min(1, Number.isFinite(amount) ? amount : 0));
  const radius = options.radius;
  if (!focus || !strength || !Number.isFinite(radius) || radius <= 0) return result;
  const maximum = Math.max(0, options.maxDisplacement ?? radius * .12) * strength;
  const active = new Map<string, {origin: Point; weight: number; group: GroupBounds}>();
  const orderedGroups = [...groups].sort((a,b)=>a.id.localeCompare(b.id));
  for (const group of orderedGroups) for (const id of [...group.nodeIds].sort()) {
    const origin = base.get(id);
    if (!origin) continue;
    const dx = origin.x - focus.x, dy = origin.y - focus.y, distance = Math.hypot(dx,dy);
    if (distance >= radius) continue;
    const weight = (1 - distance / radius) ** 2;
    active.set(id,{origin,weight,group});
    result.set(id,{x:origin.x+dx*.2*strength*weight,y:origin.y+dy*.2*strength*weight});
  }
  const constrain = (id: string) => {
    const entry = active.get(id)!;
    const point = result.get(id)!;
    const margin = 96 * (entry.group.recordScale || 1);
    const left = entry.group.x + entry.group.contentPadding + margin;
    const right = entry.group.x + entry.group.width - entry.group.contentPadding - margin;
    const top = entry.group.y + entry.group.headerHeight + margin;
    const bottom = entry.group.y + entry.group.height - entry.group.contentPadding - margin;
    // Origins are already inside their group; projecting back along the allowed
    // displacement also keeps small or saved groups within their existing body.
    point.x = Math.max(Math.min(entry.origin.x,left),Math.min(Math.max(entry.origin.x,right),point.x));
    point.y = Math.max(Math.min(entry.origin.y,top),Math.min(Math.max(entry.origin.y,bottom),point.y));
    const dx=point.x-entry.origin.x,dy=point.y-entry.origin.y,distance=Math.hypot(dx,dy);
    const limit=maximum * Math.sqrt(entry.weight);
    if(distance>limit&&distance>0){point.x=entry.origin.x+dx*limit/distance;point.y=entry.origin.y+dy*limit/distance;}
  };
  for (const id of active.keys()) constrain(id);
  const gap = Math.max(0, options.cardGap || 0);
  if (gap) for (let pass=0;pass<12;pass++) {
    for (const group of orderedGroups) {
      const ids=[...group.nodeIds].filter(id=>base.has(id)).sort();
      for(let i=0;i<ids.length;i++)for(let j=i+1;j<ids.length;j++){
        const idA=ids[i],idB=ids[j],entryA=active.get(idA),entryB=active.get(idB);
        if(!entryA&&!entryB)continue;
        const a=result.get(idA)!,b=result.get(idB)!;
        let dx=b.x-a.x,dy=b.y-a.y,distance=Math.hypot(dx,dy);
        let deficit=gap-distance;
        if(options.cardHeight && options.cardHeight>0){
          const horizontal=gap-Math.abs(dx),vertical=options.cardHeight-Math.abs(dy);
          if(horizontal<=0||vertical<=0)continue;
          // Pick the axis from the stable cloud, never from moving positions.
          // A camera/lens change cannot suddenly flip a pair's separation axis.
          const originA=base.get(idA)!,originB=base.get(idB)!;
          const baseX=originB.x-originA.x,baseY=originB.y-originA.y;
          if(gap-Math.abs(baseX)<options.cardHeight-Math.abs(baseY)){dx=baseX===0?(i%2?1:-1):Math.sign(baseX);dy=0;deficit=horizontal;}
          else{dy=baseY===0?(j%2?1:-1):Math.sign(baseY);dx=0;deficit=vertical;}
          distance=1;
        }else{
          if(distance>=gap)continue;
          if(distance<.00001){dx=Math.cos((i+j)*2.399963);dy=Math.sin((i+j)*2.399963);distance=1;}
        }
        const push=deficit*.45*strength;
        const weightA=entryA?.weight||0,weightB=entryB?.weight||0,total=weightA+weightB;
        if(entryA){result.set(idA,{x:a.x-dx/distance*push*weightA/total,y:a.y-dy/distance*push*weightA/total});constrain(idA);}
        if(entryB){result.set(idB,{x:b.x+dx/distance*push*weightB/total,y:b.y+dy/distance*push*weightB/total});constrain(idB);}
      }
    }
  }
  return result;
}
