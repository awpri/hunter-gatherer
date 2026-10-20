/** File identity comes from the source filename and MIME type, never its display title. */
export interface FileMetadata {
  media_type?: string | null
  source_path?: string | null
  file_name?: string | null
  title?: string | null
}

export type FileTypeId = 'pdf' | 'word' | 'slides' | 'sheets' | 'text' | 'markdown' | 'image' | 'audio' | 'video' | 'code' | 'archive' | 'unknown'

export interface FileTypeDescriptor {
  id: FileTypeId
  label: string
  color: string
  extension: string
  mimeType?: string
}

const TYPES: Record<FileTypeId, { label: string; color: string }> = {
  pdf: { label: 'PDF document', color: '#d34b4b' },
  word: { label: 'Word document', color: '#3974bd' },
  slides: { label: 'Presentation', color: '#c97336' },
  sheets: { label: 'Spreadsheet', color: '#388566' },
  text: { label: 'Text document', color: '#65758a' },
  markdown: { label: 'Markdown document', color: '#65758a' },
  image: { label: 'Image', color: '#956bb4' },
  audio: { label: 'Audio', color: '#b76b98' },
  video: { label: 'Video', color: '#886ab1' },
  code: { label: 'Code', color: '#528894' },
  archive: { label: 'Archive', color: '#a58b4a' },
  unknown: { label: 'File', color: '#7a8290' },
}

const EXTENSIONS: Record<string, FileTypeId> = {}
for (const [id, extensions] of Object.entries({
  pdf: ['pdf'], word: ['doc', 'docx', 'dot', 'dotx', 'odt', 'rtf'],
  slides: ['ppt', 'pptx', 'pot', 'potx', 'pps', 'ppsx', 'odp', 'key'],
  sheets: ['xls', 'xlsx', 'xlsm', 'xltx', 'csv', 'tsv', 'ods', 'numbers'],
  text: ['txt', 'text', 'log'], markdown: ['md', 'markdown', 'mdown'],
  image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'svg', 'heic', 'heif', 'tif', 'tiff', 'bmp', 'ico', 'raw', 'dng', 'psd', 'ai'],
  audio: ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg', 'opus', 'aiff', 'aif', 'wma', 'mid', 'midi'],
  video: ['mp4', 'mov', 'webm', 'mkv', 'avi', 'm4v', 'mpeg', 'mpg', 'wmv', '3gp'],
  code: ['js', 'jsx', 'ts', 'tsx', 'py', 'html', 'css', 'scss', 'json', 'xml', 'yaml', 'yml', 'toml', 'sql', 'sh', 'bash', 'zsh', 'c', 'cpp', 'h', 'hpp', 'rs', 'go', 'java', 'kt', 'swift', 'rb', 'php', 'ipynb'],
  archive: ['zip', 'gz', 'tar', 'tgz', 'bz2', 'xz', '7z', 'rar', 'zst'],
})) {
  for (const extension of extensions) EXTENSIONS[extension] = id as FileTypeId
}

const MIMES: Record<string, [FileTypeId, string]> = {
  'application/pdf': ['pdf', 'PDF'],
  'application/msword': ['word', 'DOC'],
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ['word', 'DOCX'],
  'application/vnd.ms-powerpoint': ['slides', 'PPT'],
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': ['slides', 'PPTX'],
  'application/vnd.ms-excel': ['sheets', 'XLS'],
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['sheets', 'XLSX'],
  'text/csv': ['sheets', 'CSV'],
  'text/tab-separated-values': ['sheets', 'TSV'],
  'text/plain': ['text', 'TXT'],
  'text/markdown': ['markdown', 'MD'],
  'text/x-markdown': ['markdown', 'MD'],
  'application/json': ['code', 'JSON'],
  'application/xml': ['code', 'XML'],
  'text/html': ['code', 'HTML'],
  'text/css': ['code', 'CSS'],
  'application/javascript': ['code', 'JS'],
  'text/javascript': ['code', 'JS'],
  'application/zip': ['archive', 'ZIP'],
  'application/gzip': ['archive', 'GZ'],
  'application/x-tar': ['archive', 'TAR'],
  'application/x-7z-compressed': ['archive', '7Z'],
  'application/vnd.rar': ['archive', 'RAR'],
  'application/x-rar-compressed': ['archive', 'RAR'],
}

function sourceExtension(metadata: FileMetadata): string {
  for (const source of [metadata.file_name, metadata.source_path]) {
    const filename = (source || '').split(/[\\/]/).pop() || ''
    const dot = filename.lastIndexOf('.')
    if (dot > 0) {
      const extension = filename.slice(dot + 1).toLowerCase()
      if (/^[a-z0-9]{1,12}$/.test(extension)) return extension
    }
  }
  return ''
}

export function fileType(metadata: FileMetadata = {}): FileTypeDescriptor {
  const extension = sourceExtension(metadata)
  const mimeType = (metadata.media_type || '').split(';')[0].trim().toLowerCase()
  const mime = MIMES[mimeType]
  let id: FileTypeId = EXTENSIONS[extension] || mime?.[0] || 'unknown'
  if (id === 'unknown') {
    if (mimeType.startsWith('image/')) id = 'image'
    else if (mimeType.startsWith('audio/')) id = 'audio'
    else if (mimeType.startsWith('video/')) id = 'video'
    else if (mimeType.startsWith('text/')) id = 'text'
  }
  return {
    id, ...TYPES[id],
    extension: extension.toUpperCase() || mime?.[1] || '',
    ...(mimeType ? { mimeType } : {}),
  }
}
