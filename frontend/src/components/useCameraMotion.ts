import {useCallback, useRef, useState} from 'react';
import {zoomAt, type CameraView, type Point} from './graphGeometry';
export function useCameraMotion(_reduced: boolean, initialView?: CameraView) {
  const [view, setView] = useState<CameraView>(initialView || {x: 0, y: 0, scale: 1});
  const viewRef = useRef(view);
  const publish = useCallback((next: CameraView) => {viewRef.current = next; setView(next);}, []);
  // Navigation is immediate in every motion mode; no targets remain queued.
  const cancelCamera = useCallback(() => {}, []);
  const moveCamera = useCallback((target: CameraView, _animate = true) => {
    cancelCamera(); publish(target);
  }, [cancelCamera, publish]);
  const zoomCamera = useCallback((factor: number, point: Point, minScale: number, maxScale = 4, _accelerate = false) => {
    if (!Number.isFinite(factor) || factor <= 0) return;
    cancelCamera(); publish(zoomAt(viewRef.current, factor, point, minScale, maxScale));
  }, [cancelCamera, publish]);
  const panCamera = useCallback((dx: number, dy: number) => {
    if (!Number.isFinite(dx) || !Number.isFinite(dy) || (dx === 0 && dy === 0)) return;
    cancelCamera(); publish({...viewRef.current, x: viewRef.current.x + dx, y: viewRef.current.y + dy});
  }, [cancelCamera, publish]);

  return {view, viewRef, moveCamera, cancelCamera, zoomCamera, panCamera};
}
