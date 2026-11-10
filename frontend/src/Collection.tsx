import { useMemo, useState } from 'react'
import { api, type Card, type Schema } from './api'
import { ConfirmDialog } from './ConfirmDialog'
import { ReleaseCard } from './ReleaseCard'

export type CollectionFilters = { query: string, status: string, chat: string, genre: string, sort: string, aoty: string, rym: string, score: string }
export const initialCollectionFilters: CollectionFilters = { query: '', status: 'all', chat: '', genre: '', sort: 'recent', aoty: '', rym: '', score: '' }
const normalize = (value: string) => value.toLocaleLowerCase('ru').replaceAll('ё', 'е').trim()
// Normalize within each source's own scale; never combine AOTY and RYM ratings.
export function sourceRating(card: Card, source: string): number | null {
  const values = card.historical_ratings.filter(rating => rating.source.toUpperCase() === source && rating.rating != null && rating.scale > 0)
    .map(rating => rating.rating! / rating.scale * (source === 'AOTY' ? 100 : 5))
  return values.length ? Math.max(...values) : null
}
export function selectCollection(cards: Card[], filters: CollectionFilters): Card[] {
  const terms = normalize(filters.query).split(/\s+/).filter(Boolean)
  const selected = cards.filter(card => {
    const text = normalize([card.title, ...card.artists].join(' '))
    const aoty = sourceRating(card, 'AOTY'), rym = sourceRating(card, 'RYM')
    return terms.every(term => text.includes(term))
      && (filters.status === 'all' || (filters.status === 'listened' ? card.listened : card.feedback === filters.status))
      && (!filters.chat || (filters.chat === 'unlinked' ? !card.chats?.length : card.chats?.some(chat => chat.id === filters.chat)))
      && (!filters.genre || (filters.genre === 'unknown' ? !card.genres.length : card.genres.includes(filters.genre)))
      && (!filters.aoty || (aoty !== null && aoty >= Number(filters.aoty)))
      && (!filters.score || (card.score != null && card.score >= Number(filters.score)))
      && (!filters.rym || (rym !== null && rym >= Number(filters.rym)))
  })
  return selected.sort((a, b) => {
    if (filters.sort === 'title') return a.title.localeCompare(b.title, 'ru', { numeric: true })
    if (filters.sort === 'artist') return a.artists.join(', ').localeCompare(b.artists.join(', '), 'ru')
    if (filters.sort === 'aoty' || filters.sort === 'rym') return (sourceRating(b, filters.sort.toUpperCase()) ?? -1) - (sourceRating(a, filters.sort.toUpperCase()) ?? -1)
    if (filters.sort === 'score') return (b.score ?? -1) - (a.score ?? -1)
    if (filters.sort === 'year') return (b.year ?? -1) - (a.year ?? -1)
    return (b.saved_at ?? '').localeCompare(a.saved_at ?? '')
  })
}

export function Collection({ cards, onFeedback, onReset, onOpenChat, onDiscover }: {
  cards: Card[], onFeedback: (id: string, kind: Schema['FeedbackRequest']['kind'], score: number | null, review: string | null) => void,
  onReset: (id: string | undefined, forget: boolean) => void, onOpenChat: (id: string) => void, onDiscover: () => void,
}) {
  const [filters, setFilters] = useState(initialCollectionFilters)
  const [groupBy, setGroupBy] = useState('chat')
  const [limit, setLimit] = useState(24)
  const [removing, setRemoving] = useState<Card | 'all' | null>(null)
  const [forget, setForget] = useState(false)
  function requestRemoval(value: Card | 'all') { setForget(false); setRemoving(value) }
  const change = (key: keyof CollectionFilters, value: string) => { setFilters(current => ({ ...current, [key]: value })); setLimit(24) }
  const genres = [...new Set(cards.flatMap(card => card.genres))].sort((a, b) => a.localeCompare(b, 'ru'))
  const chats = [...new Map(cards.flatMap(card => card.chats ?? []).map(chat => [chat.id, chat])).values()]
  const selected = useMemo(() => selectCollection(cards, filters), [cards, filters])
  const groups = new Map<string, { title: string, cards: Card[] }>()
  for (const card of selected.slice(0, limit)) {
    const keys = groupBy === 'chat' ? (card.chats?.length ? card.chats.map(chat => ({ id: chat.id, title: chat.title })) : [{ id: 'unlinked', title: 'Без чата' }])
      : groupBy === 'genre' ? (card.genres.length ? card.genres.map(genre => ({ id: genre, title: genre })) : [{ id: 'unknown', title: 'Жанр не указан' }])
      : [{ id: 'all', title: '' }]
    for (const key of keys) {
      if (groupBy === 'chat' && filters.chat && key.id !== filters.chat) continue
      if (groupBy === 'genre' && filters.genre && key.id !== filters.genre) continue
      if (!groups.has(key.id)) groups.set(key.id, { title: key.title, cards: [] })
      groups.get(key.id)!.cards.push(card)
    }
  }
  function resetFilters() { setFilters(initialCollectionFilters); setGroupBy('chat'); setLimit(24) }
  async function remove() {
    const id = removing && removing !== 'all' ? removing.release_id : undefined
    await api(`${id ? `/collection/${encodeURIComponent(id)}` : '/collection'}?forget=${forget}`, 'DELETE')
    onReset(id, forget)
    if (!id) resetFilters()
  }
  return <section className="collection-view" aria-labelledby="collection-title">
    <div className="collection-heading"><div><h1 id="collection-title">Моя коллекция</h1><p className="muted">Отложенные и прослушанные альбомы из ваших диалогов</p></div><button className="text-button danger-text" disabled={!cards.length} onClick={() => requestRemoval('all')}>Очистить коллекцию</button></div>
    <div className="collection-search-row">
      <label className="collection-search">Поиск по коллекции<input type="search" placeholder="Альбом или исполнитель" value={filters.query} onChange={event => change('query', event.target.value)} /></label>
    </div><details className="collection-options"><summary>Фильтры и группировка</summary><div className="collection-toolbar">
      <label>Показать<select aria-label="Показать" value={filters.status} onChange={event => change('status', event.target.value)}><option value="all">Все релизы</option><option value="listened">Прослушанные</option><option value="save_for_later">Послушать позже</option><option value="listened_like">Понравилось</option><option value="listened_mixed">Смешанные впечатления</option><option value="listened_dislike">Не понравилось</option><option value="known">Уже знаю</option></select></label>
      <label>Сортировка<select aria-label="Сортировка" value={filters.sort} onChange={event => change('sort', event.target.value)}><option value="recent">Сначала недавно отмеченные</option><option value="title">Название: А–Я</option><option value="artist">Исполнитель: А–Я</option><option value="aoty">Оценка AOTY: выше сначала</option><option value="rym">Оценка RYM: выше сначала</option><option value="score">Моя оценка: выше сначала</option><option value="year">Год: новые сначала</option></select></label>

      <label>Чат<select aria-label="Чат" value={filters.chat} onChange={event => change('chat', event.target.value)}><option value="">Все чаты</option>{chats.map(chat => <option key={chat.id} value={chat.id}>{chat.title}</option>)}<option value="unlinked">Без чата</option></select></label>
      <label>Жанр<select aria-label="Жанр" value={filters.genre} onChange={event => change('genre', event.target.value)}><option value="">Все жанры</option>{genres.map(genre => <option key={genre}>{genre}</option>)}<option value="unknown">Жанр не указан</option></select></label>
      <label>Группировать<select aria-label="Группировать" value={groupBy} onChange={event => { setGroupBy(event.target.value); setLimit(24) }}><option value="none">Единым списком</option><option value="chat">По чатам</option><option value="genre">По жанрам</option></select></label>
      <label>AOTY от (из 100)<input type="number" min="0" max="100" step="1" value={filters.aoty} onChange={event => change('aoty', event.target.value)} placeholder="Любая оценка" /></label>
      <label>RYM от (из 5)<input type="number" min="0" max="5" step="0.1" value={filters.rym} onChange={event => change('rym', event.target.value)} placeholder="Любая оценка" /></label>
      <label>Моя оценка от (из 100)<input type="number" min="0" max="100" step="1" value={filters.score} onChange={event => change('score', event.target.value)} placeholder="Любая оценка" /></label>
    </div><p className="muted">AOTY и RYM — исторические оценки других слушателей, «Моя оценка» — ваша собственная. При сортировке альбомы без оценки идут в конце; при заданном минимуме — скрываются. Один альбом может относиться к нескольким чатам или жанрам.</p></details>
    <div className="collection-results"><span role="status">Найдено: {selected.length} из {cards.length}</span><button className="text-button" onClick={resetFilters}>Сбросить фильтры</button></div>
    {[...groups].map(([id, group]) => <section className="collection-group" key={id} aria-label={group.title || 'Альбомы коллекции'}><h2 className={group.title ? 'collection-group-title' : 'sr-only'}>{groupBy === 'chat' && chats.some(chat => chat.id === id && !chat.deleted) ? <button className="text-button" onClick={() => onOpenChat(id)} title="Открыть чат">{group.title}</button> : group.title || 'Альбомы коллекции'}</h2><div className="collection-grid">{group.cards.map(card => <div className="collection-entry" data-release-id={card.release_id} key={card.release_id}>
      {groupBy !== 'chat' && <div className="collection-origins"><span>Из чатов:</span>{card.chats?.length ? card.chats.map(origin => origin.deleted ? <span key={origin.id}>Удалённый чат</span> : <button key={origin.id} className="text-button" onClick={() => onOpenChat(origin.id)}>{origin.title}</button>) : <span>Без чата</span>}</div>}
      <ReleaseCard card={card} onFeedback={onFeedback}/><button className="collection-remove text-button danger-text" onClick={() => requestRemoval(card)}>Убрать из коллекции</button>
    </div>)}</div></section>)}
    {selected.length > limit && <button className="collection-more" onClick={() => setLimit(current => current + 24)}>Показать ещё ({selected.length - limit})</button>}
    {!selected.length && <div className="empty-collection"><h2>{cards.length ? 'Ничего не найдено' : 'Здесь будет ваша музыка'}</h2><p>{cards.length ? 'Попробуйте другой запрос или сбросьте фильтры.' : 'Сохраняйте и оценивайте релизы в чате.'}</p><button onClick={cards.length ? resetFilters : onDiscover}>{cards.length ? 'Показать всю коллекцию' : 'Вернуться к открытиям'}</button></div>}
    {removing && <ConfirmDialog title={removing === 'all' ? 'Очистить всю коллекцию?' : `Убрать «${removing.title}»?`} description={`${removing === 'all' ? `Будут убраны все ${cards.length} альбомов, включая скрытые фильтрами.` : 'Альбом будет убран из коллекции во всех чатах.'} Переписка сохранится.`} confirmLabel={forget ? 'Удалить и забыть' : 'Убрать из коллекции'} onClose={() => setRemoving(null)} onConfirm={remove}>
      <label className="checkbox-label"><input type="checkbox" checked={forget} onChange={event => setForget(event.target.checked)}/>Удалить из памяти</label>
      <p className="muted">{forget ? 'Оценки и отметки «Уже знаю» сбросятся. Альбомы снова смогут попадать в новые диалоги. Отменить сброс нельзя.' : 'Оценки сохранятся, а альбомы больше не будут рекомендоваться — даже если были только отложены на потом.'}</p>
    </ConfirmDialog>}</section>
}
