import { fileType, type FileMetadata, type FileTypeDescriptor, type FileTypeId } from '../lib/fileTypes'

export interface FileTypeIconProps {
  descriptor?: FileTypeDescriptor
  metadata?: FileMetadata
  size?: number
  showExtension?: boolean
  className?: string
}

function Mark({ id }: { id: FileTypeId }) {
  switch (id) {
    case 'pdf': return <path d="M9 23c4-1 6-5 7-11 0-3-2-2-2 0 0 5 3 8 7 9 3 1 4-2 1-2-4 0-9 2-12 5-2 2-3 0-1-1Z" />
    case 'word': return <path d="m9 14 2.5 9 3-7 3 7 2.5-9" />
    case 'slides': return <><rect x="8" y="13" width="15" height="10" rx="1" /><path d="M15.5 23v3m-4 0h8M11 20l3-3 3 2 3-4" /></>
    case 'sheets': return <><rect x="8" y="13" width="15" height="12" rx="1" /><path d="M8 17h15M8 21h15m-10-8v12m5-12v12" /></>
    case 'markdown': return <><path d="M7 23v-9l4 5 4-5v9m3-5 3 4 3-4m-3-4v8" /></>
    case 'text': return <path d="M9 14h13M9 18h13M9 22h9" />
    case 'image': return <><rect x="8" y="13" width="15" height="12" rx="1" /><circle cx="18.5" cy="16.5" r="1" /><path d="m8 23 5-6 5 6 2-3 3 3" /></>
    case 'audio': return <><path d="M14 23V13l8-2v10m-8-6 8-2" /><ellipse cx="11.5" cy="23.5" rx="2.5" ry="2" /><ellipse cx="19.5" cy="21.5" rx="2.5" ry="2" /></>
    case 'video': return <><rect x="8" y="13" width="15" height="12" rx="1" /><path d="m14 16 5 3-5 3Z" /></>
    case 'code': return <path d="m11 14-4 5 4 5m10-10 4 5-4 5m-4-11-3 12" />
    case 'archive': return <><path d="M15 10v3m2 0v3m-2 0v3m2 0v3" /><rect x="14" y="22" width="4" height="4" rx="1" /></>
    default: return <path d="M10 15h11m-11 4h11m-11 4h7" />
  }
}

/** Distinct glyphs, color and a written extension keep file formats recognizable. */
export function FileTypeIcon({ descriptor, metadata, size = 32, showExtension = true, className }: FileTypeIconProps) {
  const type = descriptor || fileType(metadata)
  const badge = type.extension || (type.id === 'unknown' ? 'FILE' : type.id.toUpperCase())
  const label = `${type.label}${type.extension ? ` (${type.extension})` : ''}`
  return <svg
    className={`ph-file-icon${className ? ` ${className}` : ''}`}
    width={size}
    height={size * 38 / 32}
    viewBox="0 0 32 38"
    role="img"
    aria-label={label}
    style={{ display: 'inline-block', flexShrink: 0, verticalAlign: 'middle', color: type.color }}
  >
    <title>{label}</title>
    <path d="M5 2h14l8 8v24H5Z" fill="currentColor" fillOpacity=".09" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    <path d="M19 2v8h8" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    <g fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><Mark id={type.id} /></g>
    {showExtension && <>
      <rect x="1" y="27" width="30" height="10" rx="2" fill="currentColor" />
      <text x="16" y="34.2" textAnchor="middle" fill="white" fontSize={badge.length > 5 ? 5.4 : 6.8} fontWeight="700" fontFamily="ui-sans-serif, system-ui, sans-serif" letterSpacing=".3">{badge.slice(0, 12)}</text>
    </>}
  </svg>
}

export default FileTypeIcon
