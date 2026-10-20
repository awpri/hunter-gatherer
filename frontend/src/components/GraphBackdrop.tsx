import {useId} from 'react';
import type {CameraView} from './graphGeometry';

export function backdropMetrics(view: CameraView, overviewScale: number) {
  const zoom = view.scale / Math.max(.000001, overviewScale);
  const t = Math.max(0, Math.min(1, (zoom - .65) / 1.55));
  return {spacing: Math.max(1, 24 * zoom), opacity: .48 * t * t * (3 - 2 * t), radius: Math.min(1.5, Math.max(.5, .8 * Math.sqrt(zoom)))};
}

/** A world-anchored drafting surface: quieter at a distance, clearer up close. */
export function GraphBackdrop({view, overviewScale, width, height}: {view: CameraView; overviewScale: number; width: number; height: number}) {
  const id = useId().replace(/:/g, ''), {spacing, opacity, radius} = backdropMetrics(view, overviewScale);
  return <g className="cm-backdrop" opacity={opacity} pointerEvents="none" aria-hidden="true">
    <defs>
      <pattern id={`${id}-dots`} patternUnits="userSpaceOnUse" x={view.x} y={view.y} width={spacing} height={spacing}><circle cx={spacing / 2} cy={spacing / 2} r={radius} fill="var(--muted)" /></pattern>
      <pattern id={`${id}-grid`} patternUnits="userSpaceOnUse" x={view.x} y={view.y} width={spacing} height={spacing}><path d={`M ${spacing} 0 H 0 V ${spacing}`} fill="none" stroke="var(--muted)" strokeWidth={.65} /></pattern>
    </defs>
    <rect className="cm-backdrop-dots" width={width} height={height} fill={`url(#${id}-dots)`} />
    <rect className="cm-backdrop-grid" width={width} height={height} fill={`url(#${id}-grid)`} />
  </g>;
}
