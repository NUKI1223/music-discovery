import { useEffect, useState } from 'react'
import { api, type Schema } from './api'

/** Show the actual request-local wishes before the user confirms them. */
export function RequestHints({ filters, onClear }: { filters: Schema['Filters'], onClear?: () => void }) {
  const requested = filters.requested_artist_ids ?? []
  const reference = filters.reference_artist_ids ?? []
  const excluded = filters.excluded_artist_ids ?? []
  const preferred = filters.preferred_descriptors ?? []
  const avoided = filters.avoided_descriptors ?? []
  const ids = [...new Set([...reference, ...excluded, ...requested])].join(',')
  const [artists, setArtists] = useState<Schema['Lookup'][]>([])
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let active = true
    setArtists([]); setFailed(false)
    const allIds = ids ? ids.split(',') : []
    const batches = Array.from({ length: Math.ceil(allIds.length / 50) }, (_, i) => allIds.slice(i * 50, (i + 1) * 50))
    if (ids) Promise.all(batches.map(batch => api<Schema['Lookup'][]>(`/catalog/artists?ids=${encodeURIComponent(batch.join(','))}`)))
      .then(items => { if (active) setArtists(items.flat()) })
      .catch(() => { if (active) setFailed(true) })
    return () => { active = false }
  }, [ids])
  if (!ids && !preferred.length && !avoided.length) return null
  const names = (values: string[]) => values.map(id => artists.find(a => a.id === id)?.name ?? (failed ? 'Имя недоступно' : 'Загрузка имени…')).join(', ')
  return <div className="request-hints">
    {requested.length > 0 && <p>Только эти исполнители: {names(requested)}.</p>}
    {reference.length > 0 && <p>Ориентиры этой подборки: {names(reference)}.</p>}
    {excluded.length > 0 && <p>Не предлагать: {names(excluded)}.</p>}
    {preferred.length > 0 && <p>Предпочитать признаки: {preferred.join(', ')}.</p>}
    {avoided.length > 0 && <p>Меньше признаков: {avoided.join(', ')}.</p>}
    {(preferred.length > 0 || avoided.length > 0) && <small>Это мягкие пожелания по описаниям источников. Они не гарантируют точное звучание.</small>}
    {onClear && <button type="button" className="text-button" onClick={onClear}>Сбросить ориентиры и пожелания</button>}
  </div>
}
