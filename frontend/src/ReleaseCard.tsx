import { useEffect, useState } from 'react'
import { albumLink, releaseMedia, safeLink } from './releaseMedia'
import { api, messageOf, type Card, type Schema } from './api'
import { Icon } from './Interface'
const feedbackLabels: Record<Schema['FeedbackRequest']['kind'], string> = { known: 'Уже знаю', save_for_later: 'Послушать позже', listened_like: 'Прослушал — нравится', listened_dislike: 'Прослушал — не нравится' }
export function ReleaseCard({ card, onFeedback, onExplain }: { card: Card, onFeedback: (id: string, feedback: Schema['FeedbackRequest']['kind']) => void, onExplain?: (card: Card) => void }) {
  const [failedArtwork, setFailedArtwork] = useState<string>()
  const [resolved, setResolved] = useState<Schema['Artwork']>()
  const media = releaseMedia(card, resolved?.listening_url, resolved?.listening_links)
  const coverLink = (resolved?.source_url && safeLink(resolved.source_url)) || (resolved?.listening_url && albumLink(resolved.listening_url)?.url)
  const [failedResolved, setFailedResolved] = useState<string>()
  const hasAoty = card.links.some(link => { try { return new URL(link.url).hostname === 'www.albumoftheyear.org' } catch { return false } })
  const needsLookup = true // Resolve immediately; archive.org may hang before onError fires.
  useEffect(() => {
    setResolved(undefined); setFailedResolved(undefined)
    if (!needsLookup) return
    let active = true
    api<Schema['Artwork']>(`/releases/${encodeURIComponent(card.release_id)}/artwork`)
      .then(result => { if (active) setResolved(result) }).catch(() => { if (active) setResolved({ listening_service: null, artwork: null, listening_url: null, source_url: null, aoty_status: hasAoty ? "unavailable" : null }) })
    return () => { active = false }
  }, [card.release_id, needsLookup, hasAoty])
  const artwork = (needsLookup ? resolved?.artwork : undefined) || (failedArtwork !== media.artwork ? media.artwork : undefined)
  const [loadedArtwork, setLoadedArtwork] = useState<string>()
  useEffect(() => {
    if (!artwork || loadedArtwork === artwork) return
    const timer = window.setTimeout(() => {
      if (artwork === media.artwork) setFailedArtwork(artwork)
      else setFailedResolved(artwork)
    }, 8000)
    return () => window.clearTimeout(timer)
  }, [artwork, media.artwork, loadedArtwork])
  const cover = artwork && failedResolved !== artwork
    ? <img src={artwork} alt={`Обложка ${card.artists.join(', ')} — ${card.title}`} decoding="async" onLoad={() => setLoadedArtwork(artwork)} referrerPolicy="no-referrer" onError={() => { if (artwork === media.artwork) setFailedArtwork(artwork); else setFailedResolved(artwork) }} />
    : <div className="release-artwork-fallback"><span aria-hidden="true">{card.title.slice(0, 1).toUpperCase()}</span><small>{needsLookup && !resolved ? 'Ищем обложку…' : resolved?.aoty_status && !['found', 'not_found'].includes(resolved.aoty_status) ? 'Источник недоступен' : 'Нет обложки'}</small></div>
  const [busy, setBusy] = useState(false); const [error, setError] = useState('')
  async function feedback(kind: Schema['FeedbackRequest']['kind']) {
    if (busy) return; setBusy(true); setError('')
    try { await api('/feedback', 'POST', { feedback_id: crypto.randomUUID(), decision_id: card.decision_id, release_id: card.release_id, kind }); onFeedback(card.release_id, kind) } catch (error) { setError(messageOf(error)) } finally { setBusy(false) }
  }
  return <article className="release-card"><div className="release-top"><div className="release-artwork">{coverLink ? <a href={coverLink} target="_blank" rel="noopener noreferrer" aria-label={`Открыть ${card.title} в ${resolved?.source_url ? 'AOTY' : resolved?.listening_service || 'Apple Music'}`}>{cover}</a> : cover}</div><div><div className="eyebrow">{card.year || 'Год неизвестен'}</div><h3>{card.title}</h3><p className="artist">{card.artists.join(', ')}</p><div className="genre-tags">{card.genres.map(genre => <span key={genre}>{genre}</span>)}</div></div></div><div className="explanation">{card.explanation.map((line, index) => <p key={index}>{line}</p>)}</div>{card.warnings.length > 0 && <p className="card-warning">{card.warnings.find(warning => /учебн|вымышлен|неизвестн|не подтвержден/i.test(warning)) || card.warnings[0]}</p>}{card.warnings.length > 1 && <details className="source-details"><summary>О данных рекомендации</summary>{card.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</details>}{card.historical_ratings.length > 0 && <div className="ratings"><small>Исторические оценки · не оценка вашего вкуса</small>{card.historical_ratings.map((rating, index) => <span key={index}><strong>{rating.source}</strong> {rating.rating == null ? '—' : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(rating.rating)} / {rating.scale}{rating.count !== null && ` · ${rating.count} — счётчик в источнике`}</span>)}</div>}<div className="listening-links" aria-label={`Где послушать ${card.title}`}>
      {media.listening.length > 0 && <div><strong>Открыть альбом</strong><div className="listening-actions">{media.listening.map(link => <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}</div></div>}
      {!resolved && <small role="status" className="platform-loading"><span className="platform-spinner" aria-hidden="true"/>Ищем площадки с этим альбомом…</small>}
      {resolved && media.listening.length === 0 && <small role="status">Прямая ссылка на площадку пока не найдена.</small>}
      {resolved && Object.values(resolved.link_status || {}).some(status => ['unavailable', 'blocked', 'partial'].includes(status)) && <small>Часть площадок пока не удалось проверить.</small>}
    </div>
    <div className="source-links">{media.sources.map(link => <a href={link.url} key={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}{onExplain && <button className="text-button" onClick={() => onExplain(card)}>Почему этот альбом?</button>}</div><div className="feedback-buttons" aria-label={`Оценить ${card.title}`}>{Object.entries(feedbackLabels).map(([kind, label]) => <button key={kind} aria-pressed={card.feedback === kind} disabled={busy} className={card.feedback === kind ? 'selected' : ''} onClick={() => feedback(kind as Schema['FeedbackRequest']['kind'])}>{label}</button>)}</div>{card.listened && <p className="listened-note" role="status"><Icon name="check"/><span>В прослушанных · больше не рекомендуем</span></p>}{error && <p className="error" role="alert">{error}</p>}</article>
}
