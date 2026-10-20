import type {CameraView,CanvasSize} from '../components/graphGeometry';
export interface CameraSnapshot {view:CameraView;viewport:CanvasSize}
export function validCamera(snapshot:CameraSnapshot|undefined):snapshot is CameraSnapshot {return !!snapshot&&[snapshot.view.x,snapshot.view.y,snapshot.view.scale,snapshot.viewport.width,snapshot.viewport.height].every(Number.isFinite)&&snapshot.view.scale>0&&snapshot.viewport.width>0&&snapshot.viewport.height>0;}
export function restoreCamera(snapshot:CameraSnapshot,viewport:CanvasSize):CameraView {return {...snapshot.view,x:snapshot.view.x+(viewport.width-snapshot.viewport.width)/2,y:snapshot.view.y+(viewport.height-snapshot.viewport.height)/2};}
/** Workspace-local numeric positions only; privacy remount discards the owner. */
export class NavigationPositions {
 private cameras=new Map<string,CameraSnapshot>();private scrolls=new Map<string,number>();
 constructor(private limit=20){}
 private put<T>(map:Map<string,T>,key:string,value:T){map.delete(key);map.set(key,value);while(map.size>this.limit)map.delete(map.keys().next().value!);}
 camera(key:string){const snapshot=this.cameras.get(key);return snapshot?{view:{...snapshot.view},viewport:{...snapshot.viewport}}:undefined;}
 saveCamera(key:string,snapshot:CameraSnapshot){if(validCamera(snapshot))this.put(this.cameras,key,{view:{...snapshot.view},viewport:{...snapshot.viewport}});}
 scroll(key:string){return this.scrolls.get(key)||0;}
 saveScroll(key:string,value:number){if(Number.isFinite(value)&&value>=0)this.put(this.scrolls,key,value);}
}
