import { describe, expect, it } from 'vitest'
import { fileType } from './fileTypes'

describe('fileType', () => {
  it.each([
    ['PDF', 'pdf'], ['doc', 'word'], ['docx', 'word'], ['ppt', 'slides'], ['pptx', 'slides'],
    ['xls', 'sheets'], ['xlsx', 'sheets'], ['csv', 'sheets'], ['md', 'markdown'], ['txt', 'text'],
    ['png', 'image'], ['mp3', 'audio'], ['mov', 'video'], ['tsx', 'code'], ['zip', 'archive'],
  ])('recognizes the actual .%s filename', (extension, id) => {
    expect(fileType({ source_path: `/archive/Original.${extension}`, title: 'Unrelated title' })).toMatchObject({ id, extension: extension.toUpperCase() })
  })

  it('ignores file-looking titles and unrecognized MIME types', () => {
    expect(fileType({ title: 'Slides.pptx', media_type: 'application/octet-stream' })).toMatchObject({ id: 'unknown', extension: '' })
  })

  it('keeps legacy extensions explicit without calling them DOCX/PPTX', () => {
    expect(fileType({ file_name: 'notes.doc' }).extension).toBe('DOC')
    expect(fileType({ file_name: 'talk.ppt' }).extension).toBe('PPT')
  })

  it('uses MIME metadata when filenames are unavailable', () => {
    expect(fileType({ media_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' })).toMatchObject({ id: 'slides', extension: 'PPTX' })
    expect(fileType({ media_type: 'APPLICATION/PDF; charset=UTF-8' })).toMatchObject({ id: 'pdf', extension: 'PDF' })
    expect(fileType({ media_type: 'audio/flac' }).id).toBe('audio')
  })

  it('prefers the supplied real filename and handles Windows paths', () => {
    expect(fileType({ file_name: 'uploaded.CSV', source_path: '/tmp/source.pdf' })).toMatchObject({ id: 'sheets', extension: 'CSV' })
    expect(fileType({ source_path: 'C:\\archive\\slides.PPTX' })).toMatchObject({ id: 'slides', extension: 'PPTX' })
  })

  it('shows unknown extensions and avoids treating dotfiles as extensions', () => {
    expect(fileType({ source_path: '/tmp/source.weird' })).toMatchObject({ id: 'unknown', extension: 'WEIRD' })
    expect(fileType({ source_path: '/tmp/.hidden' }).extension).toBe('')
  })

  it('keeps the requested Office colors distinct', () => {
    const colors = ['pdf', 'docx', 'pptx', 'xlsx'].map(extension => fileType({ file_name: `source.${extension}` }).color)
    expect(new Set(colors).size).toBe(4)
  })
})
