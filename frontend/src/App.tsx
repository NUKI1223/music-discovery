import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, messageOf, setCSRF, type Card, type Chat, type Filters, type Profile, type Schema } from './api'
import { Auth, FilterFields, PasswordForm, Taste } from './Account'
import { Icon, FilterSummary, SettingsDialog } from './Interface'
import { RequestHints } from './RequestHints'
import { ReleaseCard } from './ReleaseCard'
import { Collection } from './Collection'
import { ConfirmDialog } from './ConfirmDialog'

function LoadingStatus({ label }: { label: string }) {
  return <div className="loading-status" role="status" aria-live="polite"><span className="loading-spinner" aria-hidden="true"/><span>{label}</span></div>
}

type WorkspacePage = { view: 'chat' | 'collection' | 'profile', chatId: string | null }
function readWorkspacePage(userId: string): WorkspacePage | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(`workspace-page-${userId}`) || 'null')
    if (saved && ['chat', 'collection', 'profile'].includes(saved.view)
      && (saved.chatId === null || (typeof saved.chatId === 'string' && saved.chatId.length <= 128))) return saved
  } catch { /* Browser storage is optional; invalid state starts on the default page. */ }
  return null
}

type PendingRequest = { chatId: string, body: Schema['MessageRequest'] }
// Stable server idempotency key; SHA-256 fits the API's 80-character limit.
async function proposalKey(chatId: string, proposalId: string) { const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${chatId}:${proposalId}`)); return 'filters-' + Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('') }

export default function App() {
  const [session, setSession] = useState<Schema['Session'] | null>(null)
  const [loading, setLoading] = useState(true); const [bootError, setBootError] = useState('')
  async function restore() { setLoading(true); setBootError(''); try { const result = await api<Schema['Session']>('/auth/session'); setCSRF(result.csrf_token); setSession(result) } catch (error) { if (!(error instanceof ApiError && error.status === 401)) setBootError(messageOf(error)) } finally { setLoading(false) } }
  useEffect(() => { void restore(); const expired = () => { setSession(null); setCSRF('') }; window.addEventListener('session-expired', expired); return () => window.removeEventListener('session-expired', expired) }, [])
  if (loading) return <main className="splash"><LoadingStatus label="Загружаем вашу музыку…"/></main>
  if (bootError) return <main className="splash"><p role="alert">{bootError}</p><button onClick={restore}>Повторить подключение</button></main>
  if (!session) return <Auth onSession={result => { setCSRF(result.csrf_token); setSession(result) }}/>
  return <Workspace key={session.user.id} session={session} onLogout={() => { setSession(null); setCSRF('') }}/>
}

function Workspace({ session, onLogout }: { session: Schema['Session'], onLogout: () => void }) {
  const [savedPage] = useState(() => readWorkspacePage(session.user.id))
  const [profile, setProfile] = useState<Profile | null>(null)
  const [chats, setChats] = useState<Schema['ChatSummary'][]>([])
  const [chat, setChat] = useState<Chat | null>(null)
  const [view, setView] = useState<WorkspacePage['view']>(savedPage?.view ?? 'chat')
  const [collection, setCollection] = useState<Card[]>([])
  const [applied, setApplied] = useState<Record<string, boolean>>(() => { try { return JSON.parse(sessionStorage.getItem(`applied-filters-${session.user.id}`) || '{}') } catch { return {} } })
  const [filterStatus, setFilterStatus] = useState('')
  const [settings, setSettings] = useState(false); const [draftFilters, setDraftFilters] = useState<Filters | null>(null)
  const [sidebar, setSidebar] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(() => {
    try { return localStorage.getItem(`chat-tools-${session.user.id}`) === 'open' } catch { return false }
  })
  function toggleTools() {
    const open = !toolsOpen
    setToolsOpen(open)
    try { localStorage.setItem(`chat-tools-${session.user.id}`, open ? 'open' : 'closed') } catch { /* Storage is optional. */ }
  }
  const [error, setError] = useState(''); const [text, setText] = useState('')
  const [busy, setBusy] = useState(false); const [initializing, setInitializing] = useState(true)
  const [retry, setRetry] = useState<PendingRequest | null>(null)
  const [optimisticText, setOptimisticText] = useState('')
  const [historyBusy, setHistoryBusy] = useState(false)
  const [loadingLabel, setLoadingLabel] = useState('Загружаем…')
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [memoryNotes, setMemoryNotes] = useState('')
  const historyNavigation = useRef(false)
  const [deletingChat, setDeletingChat] = useState<Schema['ChatSummary'] | null>(null)
  const lock = useRef(false); const chatRef = useRef<Chat | null>(null); chatRef.current = chat
  const chatEnd = useRef<HTMLDivElement>(null); const composer = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const resize = () => {
      const field = composer.current
      if (!field) return
      field.style.height = 'auto'
      field.style.height = `${field.value ? Math.min(field.scrollHeight, 96) : 34}px`
    }
    resize()
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [text, view, initializing, profile?.onboarded])

  async function initialize() {
    setInitializing(true); setError('')
    try {
      const [p, c] = await Promise.all([api<Profile>('/profile'), api<Schema['ChatSummary'][]>('/chats')])
      const selectedId = savedPage ? c.find(item => item.id === savedPage.chatId)?.id : c[0]?.id
      let loadedChat: Chat | null = null
      if (selectedId) {
        try { loadedChat = await api<Chat>(`/chats/${encodeURIComponent(selectedId)}`) }
        catch (error) { if (!(error instanceof ApiError && error.status === 404)) throw error }
      }
      const loadedCollection = p.onboarded && savedPage?.view === 'collection' ? await api<Card[]>('/collection') : []
      setProfile(p); setChats(c); setChat(loadedChat); chatRef.current = loadedChat; setCollection(loadedCollection)
      if (savedPage?.view === 'chat' && savedPage.chatId && !loadedChat) setError('Этот диалог больше недоступен. Выберите другой чат или создайте новый.')
    } catch (error) { setError(messageOf(error)) }
    finally { setInitializing(false) }
  }
  useEffect(() => { void initialize() }, [])
  useEffect(() => {
    if (initializing || !profile?.onboarded) return
    try {
      sessionStorage.setItem(`workspace-page-${session.user.id}`, JSON.stringify({ view, chatId: chat?.id ?? null } satisfies WorkspacePage))
    } catch { /* Navigation still works when browser storage is unavailable. */ }
  }, [view, chat?.id, initializing, profile?.onboarded, session.user.id])
  useEffect(() => { if (historyNavigation.current) { historyNavigation.current = false; document.querySelector('.conversation')?.scrollTo({ top: 0, behavior: 'instant' }); return }; chatEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [chat?.id, chat?.messages.at(-1)?.id, busy])
  async function historyPage(direction: 'older' | 'newer') {
    if (!chat || lock.current) return
    lock.current = true; setLoadingLabel('Загружаем сообщения…'); setHistoryBusy(true); setError(''); setSidebar(false)
    try {
      const cursor = direction === 'older' ? `before=${chat.oldest_seq}` : `after=${chat.newest_seq}`
      const page = await api<Chat>(`/chats/${chat.id}?limit=20&${cursor}`)
      if (page.messages.length) { historyNavigation.current = true; setChat(page) }
    } catch (error) { setError(messageOf(error)) }
    finally { lock.current = false; setHistoryBusy(false) }
  }
  async function saveMemory(reset = false) {
    if (!chat || lock.current) return
    lock.current = true; setLoadingLabel('Сохраняем память диалога…'); setHistoryBusy(true); setError(''); setSidebar(false)
    try {
      const memory = await api<Schema['ChatMemory']>(`/chats/${chat.id}/memory`, 'PUT', { notes: reset ? '' : memoryNotes, reset })
      setChat(current => current ? { ...current, memory } : current)
      setMemoryNotes(memory.notes ?? '')
    } catch (error) { setError(messageOf(error)) }
    finally { lock.current = false; setHistoryBusy(false) }
  }
  async function reloadChats() { setChats(await api<Schema['ChatSummary'][]>('/chats')) }
  async function createChat() {
    if (!profile) throw new Error('Сначала настройте профиль.')
    const created = await api<Chat>('/chats', 'POST', { title: 'Новый чат', filters: profile.filters }); setChat(created); chatRef.current = created; setView('chat'); setSettings(false); setRetry(null); await reloadChats(); return created
  }
  async function newChat() { setFilterStatus(''); setMemoryOpen(false); setExplainRelease(undefined); if (lock.current) return; lock.current = true; setLoadingLabel('Создаём новый диалог…'); setHistoryBusy(true); setError(''); setSidebar(false); try { await createChat(); setText(''); setSidebar(false); composer.current?.focus() } catch (error) { setError(messageOf(error)) } finally { lock.current = false; setHistoryBusy(false) } }
  async function selectChat(id: string) {
    if (lock.current) return
    lock.current = true; setLoadingLabel('Открываем диалог…'); setHistoryBusy(true); setError(''); setSidebar(false)
    try {
      const loaded = await api<Chat>(`/chats/${encodeURIComponent(id)}`)
      setChat(loaded); chatRef.current = loaded; setView('chat'); setSettings(false); setRetry(null); setText(''); setSidebar(false)
      setFilterStatus(''); setMemoryOpen(false); setExplainRelease(undefined)
    } catch (error) { setError(messageOf(error)) }
    finally { lock.current = false; setHistoryBusy(false) }
  }
  async function send(overrideText?: string, releaseId?: string, previous?: PendingRequest) {
    if (lock.current) return
    const requestText = overrideText ?? text.trim()
    if (!requestText && !previous) return
    lock.current = true; setBusy(true); setError(''); setRetry(null)
    let request = previous
    try {
      const current = chatRef.current ?? await createChat()
      request = previous ?? { chatId: current.id, body: { message_id: crypto.randomUUID(), text: requestText, apply_filters: false, release_id: releaseId ?? null } }
      setOptimisticText(request.body.text ?? ''); setText(''); setExplainRelease(undefined)
      const result = await api<Schema['Message']>(`/chats/${request.chatId}/messages`, 'POST', request.body)
      const updated = await api<Chat>(`/chats/${request.chatId}`); setChat(updated); await reloadChats()
      if (result.status === 'failed') setError('Помощник сейчас недоступен. Попробуйте отправить запрос позже или измените фильтры.')
      if (result.status === 'pending') setError('Запрос не завершён. Обновите историю и отправьте сообщение повторно.')
      // An interrupted message keeps its ID on the server; return the text so it can be resent as a new message.
      if (result.status === 'interrupted') { setError('Запрос был прерван. Текст возвращён в поле — отправьте его ещё раз.'); setText(request.body.text) }
    } catch (error) { setError(messageOf(error)); if (request) setRetry(request) }
    finally { setOptimisticText(''); lock.current = false; setBusy(false); composer.current?.focus() }
  }
  async function applyFilters(filters: Filters, proposalId?: string) {
    if (lock.current || (proposalId && applied[proposalId])) return
    lock.current = true; setBusy(true); setError(''); setRetry(null); setFilterStatus('Сохраняем настройки…')
    let request: PendingRequest | undefined
    try {
      const current = chatRef.current
      const updated = current
        ? await api<Chat>(`/chats/${current.id}/filters`, 'PATCH', filters)
        : await api<Chat>('/chats', 'POST', { title: 'Новый чат', filters })
      setChat(updated); chatRef.current = updated; setSettings(false)
      request = { chatId: updated.id, body: { message_id: proposalId ? await proposalKey(updated.id, proposalId) : crypto.randomUUID(), text: 'Подбери релизы по подтверждённым настройкам', apply_filters: true, release_id: null } }
      if (proposalId) { const next = { ...applied, [proposalId]: true }; setApplied(next); try { sessionStorage.setItem(`applied-filters-${session.user.id}`, JSON.stringify(next)) } catch { /* Private mode may disable storage; server idempotency still protects the request. */ } }
      setFilterStatus('Настройки применены. Подбираем релизы…'); setExplainRelease(undefined)
      const result = await api<Schema['Message']>(`/chats/${updated.id}/messages`, 'POST', request.body)
      setChat(await api<Chat>(`/chats/${updated.id}`)); await reloadChats()
      if (result.status !== 'completed') setError('Подборка не завершена. Обновите историю, чтобы проверить результат.')
      setFilterStatus(result.status === 'completed' ? 'Настройки применены. Подборка обновлена.' : 'Настройки применены. Подборка ещё не завершена.')
    } catch (error) { setError(messageOf(error)); if (request) setRetry(request); setFilterStatus(request ? 'Настройки применены. Проверьте результат тем же запросом.' : '') }
    finally { lock.current = false; setBusy(false) }
  }
  function feedback(id: string, kind: Schema['FeedbackRequest']['kind'], score: number | null, review: string | null) { const rated = kind.startsWith('listened_'); const update = (card: Card) => card.release_id === id ? { ...card, feedback: kind, score: rated ? score : null, review: rated ? review : null, saved_at: new Date().toISOString(), listened: card.listened || ['known', 'listened_like', 'listened_mixed', 'listened_dislike'].includes(kind) } : card; setCollection(current => current.map(update)); setChat(current => current ? { ...current, messages: current.messages.map(message => ({ ...message, cards: message.cards?.map(update) })) } : current) }
  function resetCollection(id: string | undefined, forget: boolean) {
    setCollection(current => id ? current.filter(card => card.release_id !== id) : [])
    const ids = new Set(id ? [id] : collection.map(card => card.release_id))
    if (forget) setChat(current => current ? { ...current, messages: current.messages.map(message => ({ ...message, cards: message.cards?.map(card => ids.has(card.release_id) ? { ...card, feedback: null, score: null, review: null, listened: false } : card) })) } : current)
  }
  async function deleteChat() {
    if (!deletingChat) return
    await api(`/chats/${encodeURIComponent(deletingChat.id)}`, 'DELETE')
    const id = deletingChat.id
    setChats(current => current.filter(item => item.id !== id))
    setCollection(current => current.map(card => ({ ...card, chats: card.chats?.map(origin => origin.id === id ? { ...origin, title: 'Удалённый чат', deleted: true } : origin) })))
    if (chatRef.current?.id === id) {
      setChat(null); chatRef.current = null; setRetry(null); setText(''); setMemoryOpen(false); setExplainRelease(undefined); setFilterStatus('')
      composer.current?.focus()
    }
  }
  async function showCollection() {
    if (lock.current) return
    lock.current = true; setLoadingLabel('Загружаем коллекцию…'); setHistoryBusy(true); setError(''); setSidebar(false)
    try { setCollection(await api<Card[]>('/collection')); setView('collection'); setSidebar(false) }
    catch (error) { setError(messageOf(error)) }
    finally { lock.current = false; setHistoryBusy(false) }
  }
  async function logout() { if (lock.current) return; try { await api('/auth/logout', 'POST'); onLogout() } catch (error) { setError(messageOf(error)) } }
  const renderCard = (card: Card, index: number) => <ReleaseCard key={`${card.decision_id}-${card.release_id}-${index}`} card={card} onFeedback={feedback} onExplain={view === 'chat' ? card => { setText(`Почему альбом «${card.title}» подходит мне?`); composer.current?.focus(); setExplainRelease(card.release_id) } : undefined}/>
  const [explainRelease, setExplainRelease] = useState<string | undefined>()
  function submit(event: FormEvent) { event.preventDefault(); void send(undefined, explainRelease); setExplainRelease(undefined) }

  if (initializing) return <main className="splash"><LoadingStatus label="Открываем вашу библиотеку…"/></main>
  if (!profile) return <main className="splash"><p role="alert">{error}</p><button onClick={initialize}>Повторить</button><button onClick={logout}>Выйти</button></main>
  if (!profile.onboarded) return <div className="onboarding"><header className="simple-header"><span className="brand">Music Discovery</span><button onClick={logout}>Выйти</button></header><main><Taste profile={profile} onSaved={setProfile}/></main></div>
  return <div className="workspace"><a href="#main" className="skip-link">Перейти к содержимому</a><aside className={`sidebar ${sidebar ? 'is-open' : ''}`} aria-label="Навигация"><a className="brand" href="/"><Icon name="disc"/> <span>Music Discovery</span></a><span className="sidebar-caption">Ваша библиотека</span><button className="new-chat" onClick={newChat} disabled={busy || historyBusy}><Icon name="plus"/> Новый чат</button><button className={`nav-button ${view === 'collection' ? 'active' : ''}`} onClick={showCollection} disabled={busy || historyBusy}><Icon name="library"/> Моя коллекция</button><div className="history-heading">Диалоги <span>{chats.length}</span></div><nav className="chat-history" aria-label="История диалогов">{chats.map(item => <div className="chat-history-row" key={item.id}><button title={item.title} className={chat?.id === item.id && view === 'chat' ? 'active' : ''} disabled={busy || historyBusy} onClick={() => selectChat(item.id)}>{item.title}</button><button className="delete-chat" aria-label={`Удалить чат «${item.title}»`} title="Удалить чат" disabled={busy || historyBusy} onClick={() => setDeletingChat(item)}><Icon name="trash"/></button></div>)}{!chats.length && <p className="muted">Здесь появятся ваши диалоги.</p>}</nav><div className="account"><div className="avatar">{session.user.username[0].toUpperCase()}</div><div><strong>{session.user.username}</strong><button className="text-button" disabled={busy || historyBusy} onClick={() => { setView('profile'); setSidebar(false) }}>Настройки вкуса</button></div><button className="logout" aria-label="Выйти из аккаунта" disabled={busy || historyBusy} onClick={logout}><Icon name="logout"/></button></div></aside><main id="main" className="main"><header className="topbar"><button className="menu-toggle" aria-label="Открыть меню" aria-expanded={sidebar} onClick={() => setSidebar(!sidebar)}><Icon name="menu"/></button><div className="chat-title"><h1 className="screen-title"><strong>{view === 'collection' ? 'Моя коллекция' : view === 'profile' ? 'Настройки профиля' : chat?.title || 'Новый диалог'}</strong></h1></div>{view === 'chat' && <button className="settings-trigger" aria-label="Настройки подборки" disabled={busy || historyBusy} aria-haspopup="dialog" aria-expanded={settings} onClick={() => { setDraftFilters(chat?.filters || profile.filters); setSettings(true) }}><Icon name="settings"/><span>Настройки подборки</span></button>}{view === 'chat' && <button className="tools-toggle" aria-label={toolsOpen ? 'Свернуть панель чата' : 'Развернуть панель чата'} aria-expanded={toolsOpen} aria-controls="chat-tools" onClick={toggleTools}><Icon name="chevron"/></button>}</header>{historyBusy && <div className="navigation-status"><LoadingStatus label={loadingLabel}/><span className="navigation-progress" aria-hidden="true"/></div>}{error && !settings && <div className="global-error" role="alert"><p>{error}</p>{retry && <button disabled={busy || historyBusy} onClick={() => send(undefined, undefined, retry)}>Проверить / повторить тот же запрос</button>}<button aria-label="Закрыть сообщение об ошибке" onClick={() => setError('')}>×</button></div>}
    {view === 'profile' ? <div className="profile-view"><Taste profile={profile} canCancel onCancel={() => setView('chat')} onSaved={result => { setProfile(result); setView('chat') }}/><PasswordForm/></div> : view === 'collection' ? <Collection cards={collection} onFeedback={feedback} onReset={resetCollection} onOpenChat={selectChat} onDiscover={() => setView('chat')}/> : <><div id="chat-tools" className="chat-tools" hidden={!toolsOpen}><div className="filter-bar"><FilterSummary filters={chat?.filters || profile.filters}/></div>{chat && <div className="chat-memory-controls"><button disabled={busy || historyBusy} aria-expanded={memoryOpen} onClick={() => { setMemoryNotes(chat.memory?.notes ?? ''); setMemoryOpen(!memoryOpen) }}>Память диалога</button><button disabled={busy || historyBusy} onClick={() => selectChat(chat.id)}>Обновить историю</button>{chat.has_older && <button disabled={busy || historyBusy} onClick={() => historyPage('older')}>Ранее</button>}{chat.has_newer && <button disabled={busy || historyBusy} onClick={() => historyPage('newer')}>Позже</button>}</div>}
    {memoryOpen && chat && <section className="settings-panel memory-panel" aria-label="Память диалога"><h2>Память этого диалога</h2><label>Цель и пожелания<textarea value={memoryNotes} maxLength={1600} onChange={e => setMemoryNotes(e.target.value)} /></label><p className="muted">Действуют только в этом чате. Новые сообщения и подтверждённые фильтры важнее старых пожеланий. Профиль вкуса и оценки не меняются.</p><div className="actions"><button disabled={busy || historyBusy} onClick={() => saveMemory()}>Сохранить пожелания</button><button disabled={busy || historyBusy} onClick={() => saveMemory(true)}>Сбросить память диалога</button></div><p className="muted">Сброс исключит прежние сообщения из контекста помощника. Переписка, оценки и карточки сохранятся.</p>{chat.memory?.latest_request && <p>Последний содержательный запрос: {chat.memory.latest_request}</p>}<details><summary>Выдержки из старой переписки ({chat.memory?.summarized_messages ?? 0} сообщений обработано)</summary>{chat.memory?.summary?.map((item, i) => <p key={i}>{String(item.excerpt ?? '')}</p>)}<small>Сохранены последние существенные выдержки; это не полный пересказ истории.</small></details></section>}
    <p id="composer-help" className="chat-tools-help">Shift + Enter — новая строка. Оценки вкуса меняются кнопками «Прослушал». ИИ: до $0,01 за сообщение · общий лимит $0,10 в день.</p></div>{filterStatus && <div className="filter-status" role="status">{filterStatus}</div>}
    <section className="conversation" aria-label="Переписка" aria-busy={busy}>{!chat?.messages.length && !busy && <div className="welcome"><p className="eyebrow">Новый диалог</p><h1>С чего начнём слушать?</h1><p>Выберите настройки подборки или расскажите,<br/>какую музыку ищете сегодня.</p><div className="suggestions">{['Подбери что-нибудь для меня', 'Хочу более неожиданное', 'Ещё варианты в моём жанре'].map(suggestion => <button key={suggestion} onClick={() => { setExplainRelease(undefined); setText(suggestion); composer.current?.focus() }}>{suggestion}</button>)}</div></div>}{chat?.messages.map(message => <div key={message.id} className={`message ${message.role}`}><div className="message-author">{message.role === 'user' ? 'Вы' : 'Music Discovery'}</div><p className="message-text">{message.text}</p>{message.status !== 'completed' && <p role="status" className="card-warning">{message.status === 'pending' ? 'Запрос ещё выполняется. Обновите историю позже.' : message.status === 'interrupted' ? 'Запрос прерван. Автоматического повтора не будет.' : 'Помощник недоступен. Попробуйте отправить запрос позже.'}</p>}{message.proposed_filters && <div className="proposal"><strong>Предлагаемые настройки</strong><FilterSummary filters={message.proposed_filters}/><RequestHints filters={message.proposed_filters}/>{applied[message.id] ? <p className="applied-status" role="status">Применены · подборка по этим настройкам запрошена</p> : <button className="primary" disabled={busy || historyBusy} onClick={() => applyFilters(message.proposed_filters as Filters, message.id)}>Применить и подобрать</button>}</div>}<div className="cards">{!!message.cards?.length && <h2 className="sr-only">Альбомы подборки</h2>}{message.cards?.map(renderCard)}</div></div>)}{busy && <div className="pending-message" role="status">{optimisticText && <p>{optimisticText}</p>}<span className="loading-dot"/> Подбираем музыку…</div>}<div ref={chatEnd}/></section><footer className="composer-area"><form className="composer" onSubmit={submit}><label className="sr-only" htmlFor="message">Сообщение помощнику</label><textarea id="message" ref={composer} rows={1} aria-describedby="composer-help" placeholder="Какую музыку ищем?" value={text} maxLength={4000} disabled={busy || historyBusy} onChange={e => { setText(e.target.value); setExplainRelease(undefined) }} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(undefined, explainRelease); setExplainRelease(undefined) } }}/><button className="send-button" type="submit" disabled={busy || historyBusy || !text.trim()} aria-label="Отправить сообщение"><Icon name="send"/></button></form>{chat?.messages.length ? <div className="composer-foot"><div className="quick-actions"><button disabled={busy || historyBusy} onClick={() => { setExplainRelease(undefined); setText('Ещё варианты'); composer.current?.focus() }}>Ещё варианты</button><button aria-label="Более неожиданное" disabled={busy || historyBusy} onClick={() => { setExplainRelease(undefined); setText('Более неожиданное'); composer.current?.focus() }}>Неожиданнее</button></div></div> : null}</footer></>}
  </main>{deletingChat && <ConfirmDialog title={`Удалить чат «${deletingChat.title}»?`} description="Переписка и память этого диалога будут удалены без возможности восстановления. Альбомы в коллекции и ваши оценки сохранятся." confirmLabel="Удалить чат" onClose={() => setDeletingChat(null)} onConfirm={deleteChat}/>} {settings && draftFilters && <SettingsDialog onClose={() => setSettings(false)}><FilterFields value={draftFilters} onChange={setDraftFilters}/>{error && <p className="error" role="alert">{error}</p>}<footer className="dialog-actions"><button onClick={() => setSettings(false)}>Отмена</button><button className="primary" disabled={busy || historyBusy} onClick={() => applyFilters(draftFilters)}>Применить и подобрать</button></footer></SettingsDialog>}</div>
}
