import {useRef} from 'react';
import type {Point} from './graphGeometry';

export type MagneticLens = {point: Point; radius: number; amount: number};

/** Apply pointer and camera targets in the same render in every motion mode.
 * A stopped view retains its last visible lens until it resumes. */
export function useMagneticLens(target: MagneticLens, _reduced: boolean, stopped: boolean): MagneticLens {
  const current = useRef(target);
  if (!stopped && (typeof document === 'undefined' || !document.hidden)) current.current = target;
  return current.current;
}
