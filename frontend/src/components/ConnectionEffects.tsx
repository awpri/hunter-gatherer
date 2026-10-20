import {useId, type CSSProperties} from 'react';
import type {GraphActivity} from '../lib/activity';
import {sourceCurve, type Point} from './graphGeometry';
import {connectionFlows, connectionIgnitions, validFlowPoints, type EffectEdge} from './connectionFlowModel';
import './ConnectionEffects.css';
export type ConnectionEffectsProps = {
  edges: readonly EffectEdge[]; positions: ReadonlyMap<string, Point>; hovered?: string | null;
  hoverType?: string; activity?: GraphActivity; cameraScale: number; stopped: boolean; reduced?: boolean;
};
/** Geometry may drift every frame; the stable flow identity keeps the CSS clock intact. */
export function ConnectionEffects({edges, positions, hovered, hoverType, activity, cameraScale, stopped, reduced = false}: ConnectionEffectsProps) {
  const prefix = useId().replace(/:/g, '');
  const scale = Math.max(.000001, cameraScale);
  const flows = connectionFlows(edges, hovered, hoverType, activity).filter(flow => validFlowPoints(flow, positions));
  return <g className={`ce-effects ${stopped ? 'ce-stopped' : ''} ${reduced ? 'ce-reduced' : ''}`} pointerEvents="none" aria-hidden="true">
    {connectionIgnitions(flows).map(flow => {
      const from = positions.get(flow.source)!;
      return <g key={flow.groupKey} className={`ce-ignition ce-${flow.kind}`} transform={`translate(${from.x} ${from.y})`} style={{'--ce-duration': `${flow.duration}s`} as CSSProperties}><circle className="ce-source-aura" r={8 / scale} /><circle className="ce-source-core" r={2.5 / scale} /></g>;
    })}
    {flows.map((flow, index) => {
      const points = validFlowPoints(flow, positions);
      if (!points) return null;
      const gradient = `${prefix}-wake-${index}`;
      const path = sourceCurve(points.from, points.to, .24, Infinity);
      const style = {'--ce-duration': `${flow.duration}s`, '--ce-delay': `${flow.delay}s`, '--ce-radius': `${12 / scale}px`} as CSSProperties;
      return <g key={flow.key} className={`ce-flow ce-${flow.kind}`} style={style} data-source={flow.source} data-target={flow.target} data-effect-kind={flow.kind}>
        <defs><linearGradient id={gradient} gradientUnits="userSpaceOnUse" x1={points.from.x} y1={points.from.y} x2={points.to.x} y2={points.to.y}><stop offset="0" stopColor="currentColor" stopOpacity=".5" /><stop offset=".65" stopColor="currentColor" /><stop offset="1" stopColor="var(--ce-hot)" /></linearGradient></defs>
        <path className="ce-guide" d={path} vectorEffect="non-scaling-stroke" />
        <g className="ce-energy">
          <path className="ce-wake ce-wake-outer" d={path} pathLength={100} vectorEffect="non-scaling-stroke" />
          <path className="ce-wake ce-wake-middle" d={path} pathLength={100} vectorEffect="non-scaling-stroke" />
          <path className="ce-wake ce-wake-inner" style={{stroke: `url(#${gradient})`}} d={path} pathLength={100} vectorEffect="non-scaling-stroke" />
          <path className="ce-wake ce-tracer" d={path} pathLength={100} vectorEffect="non-scaling-stroke" />
          <g transform={`translate(${points.to.x} ${points.to.y})`}><circle className="ce-arrival-core" r={2 / scale} /></g>
        </g>
      </g>;
    })}
  </g>;
}
