interface SearchableCommand {id?:string;label:string; description?:string; section?:string; keywords?:string[]}
export function matchSearchCommands<T extends SearchableCommand>(commands:T[],query:string,hasEvidenceAction=false):T[] {
  const terms=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const matches=commands.filter(command=>!(hasEvidenceAction&&command.id==='gather-evidence')&&terms.every(term=>[command.label,command.description,command.section,...(command.keywords||[])].join(' ').toLocaleLowerCase().includes(term)));
  // Reserve space for matching groups so commands cannot hide them.
  return [...matches.filter(item=>item.section==='Groups').slice(0,6),...matches.filter(item=>item.section!=='Groups').slice(0,8)];
}
export function nextSearchIndex(index:number,direction:1|-1,count:number) {return count>0?(index+direction+count)%count:0;}
