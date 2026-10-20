/** Arrow navigation stays inside the active group; Tab remains available to leave it. */
export function nextScopedRecord(ids:string[],current:string,key:string):string|null {
 if(!ids.length||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(key))return null;
 if(key==='Home')return ids[0];if(key==='End')return ids[ids.length-1];
 const index=Math.max(0,ids.indexOf(current));const delta=key==='ArrowLeft'||key==='ArrowUp'?-1:1;
 return ids[(index+delta+ids.length)%ids.length];
}
