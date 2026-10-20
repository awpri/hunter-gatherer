import {useEffect, useId, useRef, useState} from 'react';
import {Icon} from '../views/LibraryShared';
import {prepareVisualImage, isVisualEmoji, visualImage, VISUAL_EMOJI, VISUAL_ICONS, type VisualMetadata} from '../lib/boardApi';
import './BoardControls.css';
export function VisualIcon({icon, size = 18}: {icon?: string | null; size?: number}) {
  return isVisualEmoji(icon) ? <span className="visual-emoji" aria-hidden="true">{icon}</span> : <Icon name={icon || 'topics'} size={size}/>;
}
export function VisualPicker({value, onChange, disabled = false, onBusyChange}: {value: VisualMetadata; onChange: (value: VisualMetadata) => void; disabled?: boolean; onBusyChange?: (busy: boolean) => void}) {
  const input = useRef<HTMLInputElement>(null), ticket = useRef(0), id = useId();
  const [error, setError] = useState(''), [reading, setReading] = useState(false), [dragging, setDragging] = useState(false);
  const valueRef = useRef(value); valueRef.current = value;
  const busyRef = useRef(onBusyChange); busyRef.current = onBusyChange;
  useEffect(() => () => {ticket.current++; busyRef.current?.(false);}, []);
  async function choose(file?: File) {
    if (!file || disabled || reading) return;
    const current = ++ticket.current; setError(''); setReading(true); busyRef.current?.(true);
    try {
      const image = await prepareVisualImage(file);
      if (current === ticket.current) onChange({...valueRef.current, image});
    } catch (error) {if (current === ticket.current) {onChange({...valueRef.current, image: undefined}); setError(error instanceof Error ? error.message : 'This image could not be read.');}}
    finally {if (current === ticket.current) {setReading(false); busyRef.current?.(false); if (input.current) input.current.value = '';}}
  }
  const image = visualImage(value.image);
  return <div className="visual-picker" aria-busy={reading}>
    <span className="visual-field-label" id={`${id}-icons`}>Icon</span>
    <div className="visual-icon-options" role="group" aria-labelledby={`${id}-icons`}>
      <button type="button" className="visual-icon-choice" aria-label="Default icon" title="Default icon" aria-pressed={!value.icon} disabled={disabled || reading} onClick={() => onChange({...value, icon: undefined})}><span aria-hidden="true">—</span></button>
      {[...VISUAL_ICONS, ...VISUAL_EMOJI].map(icon => <button type="button" key={icon} className="visual-icon-choice" aria-label={`Use ${icon} icon`} title={icon} aria-pressed={value.icon === icon} disabled={disabled || reading} onClick={() => onChange({...value, icon})}><VisualIcon icon={icon}/></button>)}
    </div>
    <span className="visual-field-label">Image <small>Optional · PNG, JPEG, WebP · up to 15 MiB</small></span>
    <div className={`visual-dropzone ${dragging ? 'drag-over' : ''} ${image ? 'has-image' : ''}`} onDragOver={event => {event.preventDefault(); if (!disabled && !reading) setDragging(true);}} onDragLeave={() => setDragging(false)} onDrop={event => {event.preventDefault(); setDragging(false); if (!disabled && !reading) void choose(event.dataTransfer.files[0]);}}>
      {image && <img src={image} alt="Selected visual"/>}
      <button type="button" className="visual-upload-button" disabled={disabled || reading} onClick={() => input.current?.click()}><Icon name="upload" size={17}/><span>{reading ? 'Reading image…' : image ? 'Replace image' : 'Choose or drop an image'}</span></button>
      <input ref={input} id={`${id}-file`} type="file" accept="image/png,image/jpeg,image/webp" aria-label="Choose a visual image" className="visual-file-input" disabled={disabled || reading} onChange={event => void choose(event.target.files?.[0])}/>
    </div>
    <span className="field-note">Images are resized on this device before saving.</span>
    {image && <button type="button" className="text-button visual-remove" disabled={disabled || reading} onClick={() => {ticket.current++; onChange({...value, image: undefined}); setError('');}}>Remove image</button>}
    {error && <p className="inline-error" role="alert">{error}</p>}
  </div>;
}
