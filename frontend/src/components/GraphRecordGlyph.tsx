import {memo} from 'react';
import {fileType} from '../lib/fileTypes';
import {FileTypeIcon} from './FileTypeIcon';
import {Icon} from '../views/LibraryShared';
import type {GraphNode} from './ConnectionMap';

/** File families have their own silhouettes even before any text is revealed. */
export const GraphRecordGlyph = memo(function GraphRecordGlyph({node, size = 136}: {node: GraphNode; size?: number}) {
  if (node.image) return <image href={node.image} x={-size / 2} y={-size / 2} width={size} height={size} preserveAspectRatio="xMidYMid slice" className="cm-custom-image" />;
  if (node.icon && /^[a-z-]+$/.test(node.icon)) return <foreignObject x={-size * .4} y={-size * .4} width={size * .8} height={size * .8} pointerEvents="none"><div className="cm-custom-symbol"><Icon name={node.icon} size={size * .8} /></div></foreignObject>;
  if (node.icon) return <text textAnchor="middle" y={size * .28} fontSize={size * .7} className="cm-custom-icon">{node.icon}</text>;
  if (node.type === 'topic') return <g className="cm-memory-glyph cm-topic-glyph">
    <path d={`M ${-size * .32} 0 L 0 ${-size * .32} L ${size * .32} 0 L 0 ${size * .32} Z`} className="cm-memory-surface" />
    <path d={`M ${-size * .24} 0 H ${size * .24} M 0 ${-size * .24} V ${size * .24}`} className="cm-memory-orbit" fill="none" />
    <circle r={size * .09} className="cm-memory-core" />
    {[[-.32, 0], [0, -.32], [.32, 0], [0, .32]].map(([x, y], index) => <circle key={index} cx={size * x} cy={size * y} r={size * .055} className="cm-memory-core" />)}
  </g>;
  if (node.type !== 'document') {
    const kind = (node.kind || 'memory').toLowerCase();
    const variant = kind === 'decision' ? 'decision' : kind === 'preference' ? 'preference' : ['procedure', 'procedural'].includes(kind) ? 'procedure' : kind === 'plan' ? 'plan' : kind === 'fact' ? 'fact' : 'memory';
    return <g className={`cm-memory-glyph cm-kind-${variant} ${node.importance && node.importance >= 8 ? 'important' : ''}`} transform={`scale(${size / 100})`}>
      {variant === 'decision' ? <path d="M 0 -43 L 43 0 L 0 43 L -43 0 Z" className="cm-memory-surface" />
        : variant === 'preference' ? <rect x={-40} y={-33} width={80} height={66} rx={16} className="cm-memory-surface" />
        : variant === 'procedure' ? <path d="M -23 -39 H 23 L 45 0 L 23 39 H -23 L -45 0 Z" className="cm-memory-surface" />
        : variant === 'plan' ? <path d="M 0 -43 L 41 33 Q 43 38 37 38 H -37 Q -43 38 -41 33 Z" className="cm-memory-surface" />
        : variant === 'fact' ? <circle r={37} className="cm-memory-surface" />
        : <rect x={-37} y={-37} width={74} height={74} rx={29} className="cm-memory-surface" />}
      {variant === 'decision' ? <path d="M -16 0 L -4 12 L 18 -13" className="cm-memory-orbit" style={{animation: 'none'}} strokeLinecap="round" strokeLinejoin="round" />
        : variant === 'preference' ? <><path d="M -22 -10 H 22 M -22 12 H 22" className="cm-memory-orbit" style={{animation: 'none'}} /><circle cx={-8} cy={-10} r={6} className="cm-memory-core" /><circle cx={10} cy={12} r={6} className="cm-memory-core" /></>
        : variant === 'procedure' ? <><path d="M -19 15 L 0 -15 L 19 15" className="cm-memory-orbit" style={{animation: 'none'}} strokeLinecap="round" strokeLinejoin="round" />{[[-19, 15], [0, -15], [19, 15]].map(([x, y], index) => <circle key={index} cx={x} cy={y} r={6} className="cm-memory-core" />)}</>
        : variant === 'plan' ? <path d="M 0 -15 L 13 18 L 0 10 L -13 18 Z" className="cm-memory-core" />
        : variant === 'fact' ? <><circle r={19} className="cm-memory-orbit" /><circle r={7} className="cm-memory-core" /></>
        : <><path d="M -20 6 Q -11 -18 8 -16 Q 25 -12 20 9" className="cm-memory-orbit" style={{animation: 'none'}} strokeLinecap="round" /><circle cx={-10} cy={10} r={7} className="cm-memory-core" /><circle cx={17} cy={-13} r={5} className="cm-memory-core" /></>}
    </g>;
  }
  const format = fileType(node);
  const landscape = ['image', 'video', 'slides'].includes(format.id);
  const round = format.id === 'audio';
  const square = format.id === 'sheets' || format.id === 'archive';
  return <g className={`cm-format-glyph format-${format.id}`} style={{color: format.color}}>
    {landscape ? <rect x={-size * .6} y={-size * .4} width={size * 1.2} height={size * .8} rx={size * .09} className="cm-format-surface" /> : round ? <rect x={-size * .38} y={-size * .56} width={size * .76} height={size * 1.12} rx={size * .36} className="cm-format-surface" /> : square ? <rect x={-size * .48} y={-size * .48} width={size * .96} height={size * .96} rx={size * .07} className="cm-format-surface" /> : <path d={`M ${-size * .4} ${-size * .57} H ${size * .18} L ${size * .42} ${-size * .32} V ${size * .57} H ${-size * .4} Z`} className="cm-format-surface" />}
    {!landscape && !round && !square && <path d={`M ${size * .18} ${-size * .57} V ${-size * .32} H ${size * .42}`} fill="none" stroke="currentColor" strokeWidth={size * .035} opacity=".6" />}
    <foreignObject x={-size * .36} y={-size * .44} width={size * .72} height={size * .9} pointerEvents="none">
      <FileTypeIcon metadata={node} size={size * .72} showExtension />
    </foreignObject>
  </g>;
});
