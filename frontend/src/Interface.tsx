import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api, type Schema } from './api'
export function Icon({ name }: { name: 'settings'|'plus'|'library'|'menu'|'close'|'send'|'logout'|'disc'|'chevron'|'trash'|'check' }) {
  const paths = { check: 'M5 12l4 4L19 6', trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7', chevron: 'M6 9l6 6 6-6', settings: 'M4 7h16M4 17h16M9 4v6M15 14v6', plus: 'M12 5v14M5 12h14', library: 'M4 4v16M8 4v16M12 4v16M16 5l4 14', menu: 'M4 6h16M4 12h16M4 18h16', close: 'M6 6l12 12M18 6L6 18', send: 'M12 19V5M5 12l7-7 7 7', logout:'M9 4H4v16h5M10 12h10M16 8l4 4-4 4', disc:'M8 4a9 9 0 1 0 8 0M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6' }
  return <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]}/></svg>
}
const genreNames: Record<string,string> = { all:'Все жанры', 'hip-hop':'Хип-хоп', jazz:'Джаз', rock:'Рок', metal:'Метал', pop:'Поп', electronic:'Электронная музыка', classical:'Классическая музыка', 'hardcore hip-hop':'Хардкор-хип-хоп', hardcore:'Хардкор-хип-хоп', 'alternative hip-hop':'Альтернативный хип-хоп' }
export const genreName = (id:string, name?:string) => genreNames[id.toLowerCase()] || genreNames[name?.toLowerCase() || ''] || name || id
export const modes = { close:'Ближе к моему вкусу', explore:'Новые направления', surprise:'Более неожиданное' }
export function FilterSummary({ filters }: { filters:Schema['Filters'] }) {
  const [genres,setGenres]=useState<Schema['Lookup'][]>([])
  useEffect(()=>{let active=true;api<Schema['Lookup'][]>('/catalog/genres').then(v=>{if(active)setGenres(v)}).catch(()=>{});return()=>{active=false}},[])
  const name=(id:string)=>genreName(id,genres.find(g=>g.id===id)?.name)
  return <div className="filter-summary"><span>{name(filters.genre_scope_id || 'hip-hop')}</span><span>{modes[filters.mode || 'explore']}</span><span>{filters.obscurity==='require'?'Только подтверждённо малоизвестное':'Предпочитать малоизвестное'}</span>{filters.locked_subgenre_ids?.map(id=><span key={id}>{name(id)}</span>)}{filters.exclude_known_artists&&<span>Без знакомых исполнителей</span>}</div>
}
export function SettingsDialog({ children, onClose }: { children:ReactNode,onClose:()=>void }) {
  const dialog=useRef<HTMLDialogElement>(null)
  useEffect(()=>{const opener=document.activeElement as HTMLElement;dialog.current?.showModal();return()=>{dialog.current?.close();opener?.focus()}},[])
  return <dialog ref={dialog} className="settings-dialog" aria-labelledby="settings-title" onCancel={e=>{e.preventDefault();onClose()}}><header><div><h2 id="settings-title">Настройки подборки</h2><p className="muted">Для этого диалога. Применение сразу подберёт новые релизы.</p></div><button aria-label="Закрыть настройки" onClick={onClose}><Icon name="close"/></button></header>{children}</dialog>
}
export function ArtistPicker({ label, values, onChange, maxItems = 50 }: { label:string,values:string[],onChange:(ids:string[])=>void,maxItems?:number }) {
  const [query,setQuery]=useState('');const [results,setResults]=useState<Schema['Lookup'][]>([]);const [names,setNames]=useState<Record<string,string>>({});const [error,setError]=useState('')
  useEffect(()=>{let active=true;const timer=setTimeout(()=>{api<Schema['Lookup'][]>(`/catalog/artists?q=${encodeURIComponent(query)}`).then(items=>{if(active){setResults(items);setNames(n=>({...n,...Object.fromEntries(items.map(a=>[a.id,a.name]))}));setError('')}}).catch(()=>{if(active)setError('Поиск исполнителей недоступен.')})},200);return()=>{active=false;clearTimeout(timer)}},[query])
  useEffect(()=>{if(!values.length)return;let active=true;api<Schema['Lookup'][]>(`/catalog/artists?ids=${encodeURIComponent(values.join(','))}`).then(items=>{if(active)setNames(n=>({...n,...Object.fromEntries(items.map(a=>[a.id,a.name]))}))}).catch(()=>{});return()=>{active=false}},[values.join(',')])
  return <div className="artist-picker"><label>{label}<input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Найти исполнителя"/></label><div className="selected-artists">{values.map(id=><button type="button" className="chip selected" key={id} aria-label={`Убрать ${names[id]||'исполнителя'}`} onClick={()=>onChange(values.filter(x=>x!==id))}>{names[id]||'Загрузка имени…'}<Icon name="close"/></button>)}</div>{query&&<div className="artist-results">{results.filter(a=>!values.includes(a.id)).slice(0,8).map(a=><button type="button" key={a.id} onClick={()=>onChange([...values,a.id])} disabled={values.length>=maxItems}><Icon name="plus"/>{a.name}</button>)}{!results.length&&<small>Не найдено в каталоге.</small>}</div>}{error&&<small role="status">{error}</small>}</div>
}
