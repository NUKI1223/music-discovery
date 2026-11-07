import { expect, test, type Page } from '@playwright/test'

const timestamp = '2026-11-02T12:00:00Z'
const initialFilters = {
  genre_scope_id: 'hip-hop', mode: 'explore', obscurity: 'prefer', locked_subgenre_ids: [],
  exclude_known_artists: false, reference_artist_ids: [], excluded_artist_ids: [],
  requested_artist_ids: [], preferred_descriptors: [], avoided_descriptors: [],
}
const proposedFilters = { ...initialFilters, mode: 'surprise', locked_subgenre_ids: ['hardcore'] }
const fixtureCard = {
  release_id: 'fixture-filter-release', decision_id: 'fixture-filter-decision',
  title: 'Тестовая подборка после применения', artists: ['Тестовый исполнитель'], year: 2024,
  genres: ['Хардкор-хип-хоп'], explanation: ['Соответствует подтверждённым фильтрам.'],
  warnings: ['Учебная карточка для проверки интерфейса.'], links: [], historical_ratings: [], feedback: null,
}

type Body = Record<string, unknown>
type Call = { method: string, path: string, body: Body }
function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>(done => { resolve = done })
  return { promise, resolve }
}

async function fixture(page: Page, options: { holdPatch?: boolean, patchFailure?: boolean, loseFirstPostResponse?: boolean } = {}) {
  const calls: Call[] = []
  const patchGate = deferred()
  const filters = structuredClone(initialFilters)
  const chat = {
    id: 'fixture-filter-chat', title: 'Диалог о музыкальных открытиях', filters,
    created_at: timestamp, updated_at: timestamp,
    has_older: false, has_newer: false, oldest_seq: 1, newest_seq: 2,
    memory: { notes: '', summary: [], summarized_messages: 0, latest_request: '' },
    messages: [
      { id: 'fixture-question', role: 'user', text: 'Предложи новые направления', created_at: timestamp,
        status: 'completed', cards: [], error_code: null, proposed_filters: null },
      { id: 'fixture-proposal', role: 'assistant', text: 'Подтвердите настройки, чтобы подобрать релизы.', created_at: timestamp,
        status: 'completed', cards: [], error_code: null, proposed_filters: structuredClone(proposedFilters) },
    ] as Body[],
  }
  const profile = { onboarded: true, filters: structuredClone(initialFilters), seed_artist_ids: [], avoid_tag_ids: [], request_only_artist_ids: [] }
  const responses = new Map<string, Body>()
  let dropped = false
  const unexpected: string[] = []
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    const body = request.postData() ? request.postDataJSON() as Body : {}
    const respond = (json: unknown, status = 200) => route.fulfill({ status, json })
    if (method !== 'GET') calls.push({ method, path, body })
    if (path === '/api/auth/session') return respond({ user: { id: 'fixture-user', username: 'fixture_listener' }, csrf_token: 'fixture-csrf' })
    if (path === '/api/profile' && method === 'GET') return respond(profile)
    if (path === '/api/catalog/genres') return respond([
      { id: 'hip-hop', name: 'Hip-hop' }, { id: 'hardcore', name: 'Hardcore hip-hop' }, { id: 'jazz', name: 'Jazz' },
    ])
    if (path === '/api/catalog/artists') return respond([{ id: 'fixture-artist', name: 'Тестовый исполнитель' }])
    if (path === '/api/chats' && method === 'GET') return respond([{ ...chat, messages: undefined, memory: undefined }])
    if (path === `/api/chats/${chat.id}` && method === 'GET') return respond(chat)
    if (path === `/api/chats/${chat.id}/filters` && method === 'PATCH') {
      if (options.holdPatch) await patchGate.promise
      if (options.patchFailure) return respond({ detail: 'Не удалось сохранить тестовые фильтры.' }, 409)
      chat.filters = structuredClone(body) as typeof filters
      return respond(chat)
    }
    if (path === `/api/chats/${chat.id}/messages` && method === 'POST') {
      const id = String(body.message_id)
      let result = responses.get(id)
      if (!result) {
        result = { id, role: 'assistant', text: 'Подборка готова.', created_at: timestamp, status: 'completed',
          cards: [fixtureCard], error_code: null, proposed_filters: null }
        responses.set(id, result)
        chat.messages.push({ id: `user-${id}`, role: 'user', text: body.text, created_at: timestamp,
          status: 'completed', cards: [], error_code: null, proposed_filters: null }, result)
        chat.newest_seq = chat.messages.length
      }
      if (options.loseFirstPostResponse && !dropped) {
        dropped = true
        return route.abort('failed')
      }
      return respond(result)
    }
    if (/^\/api\/releases\/[^/]+\/artwork$/.test(path)) return respond({ artwork: null, listening_url: null, listening_service: null, source_url: null, aoty_status: null })
    unexpected.push(`${method} ${path}`)
    return respond({ detail: `Unexpected fixture request: ${method} ${path}` }, 500)
  })
  await page.goto('/')
  await expect(page.locator('.proposal')).toBeVisible()
  return { calls, chat, profile, patchGate, unexpected }
}

function applyProposal(page: Page) {
  return page.locator('.proposal').getByRole('button', { name: 'Применить и подобрать', exact: true })
}
function settingsButton(page: Page) {
  return page.locator('.topbar').getByRole('button', { name: 'Настройки подборки', exact: true })
}

test('proposal immediately becomes busy and double click makes one PATCH then one free recommendation', async ({ page }) => {
  const state = await fixture(page, { holdPatch: true })
  await applyProposal(page).evaluate((button: HTMLButtonElement) => { button.click(); button.click() })
  await expect.poll(() => state.calls.length).toBe(1)
  await expect(page.locator('.conversation')).toHaveAttribute('aria-busy', 'true')
  await expect(page.getByLabel('Сообщение помощнику')).toBeDisabled()
  await expect(applyProposal(page)).toBeDisabled()
  expect(state.calls[0]).toMatchObject({ method: 'PATCH', path: '/api/chats/fixture-filter-chat/filters', body: proposedFilters })
  state.patchGate.resolve()
  await expect(page.getByRole('heading', { name: fixtureCard.title, exact: true })).toBeVisible()
  await expect(page.locator('.conversation')).toHaveAttribute('aria-busy', 'false')
  expect(state.calls.map(call => call.method)).toEqual(['PATCH', 'POST'])
  expect(state.calls[1].path).toBe('/api/chats/fixture-filter-chat/messages')
  expect(state.calls[1].body.apply_filters).toBe(true)
  expect(typeof state.calls[1].body.message_id).toBe('string')
  expect(state.unexpected).toEqual([])
})

test('applied proposal stays inactive after reload and does not invite another application', async ({ page }) => {
  const state = await fixture(page)
  await applyProposal(page).click()
  await expect(page.getByRole('heading', { name: fixtureCard.title })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: fixtureCard.title })).toBeVisible()
  const proposal = page.locator('.proposal')
  await expect(proposal).toContainText(/Применен|Применён|Применены|применен|применён|применены/)
  expect(await applyProposal(page).count() === 0 || await applyProposal(page).isDisabled()).toBe(true)
  expect(state.calls.map(call => call.method)).toEqual(['PATCH', 'POST'])
  expect(state.unexpected).toEqual([])
})

test('failed filter PATCH gives visible error and never starts recommendation', async ({ page }) => {
  const state = await fixture(page, { patchFailure: true })
  await applyProposal(page).click()
  await expect(page.getByRole('alert')).toContainText('Не удалось сохранить тестовые фильтры.')
  await expect(applyProposal(page)).toBeEnabled()
  expect(state.calls.map(call => call.method)).toEqual(['PATCH'])
  expect(state.chat.filters).toEqual(initialFilters)
  await expect(page.locator('.release-card')).toHaveCount(0)
  expect(state.unexpected).toEqual([])
})

test('lost recommendation response retries the same free message ID without another PATCH', async ({ page }) => {
  const state = await fixture(page, { loseFirstPostResponse: true })
  await applyProposal(page).click()
  const retry = page.getByRole('button', { name: 'Проверить / повторить тот же запрос', exact: true })
  await expect(retry).toBeVisible()
  expect(state.calls.map(call => call.method)).toEqual(['PATCH', 'POST'])
  await retry.click()
  await expect(page.getByRole('heading', { name: fixtureCard.title })).toBeVisible()
  expect(state.calls.map(call => call.method)).toEqual(['PATCH', 'POST', 'POST'])
  expect(state.calls[1].body).toEqual(state.calls[2].body)
  expect(state.calls[2].body.apply_filters).toBe(true)
  await expect(page.locator('.release-card')).toHaveCount(1)
  expect(state.unexpected).toEqual([])
})

test('settings are discoverable at top, show readable Russian values and cancel unsaved draft', async ({ page }) => {
  const state = await fixture(page)
  const trigger = settingsButton(page)
  await expect(trigger).toBeVisible()
  await expect(page.locator('.filter-bar')).toContainText('Хип-хоп')
  await expect(page.locator('.proposal')).not.toContainText('surprise')
  await expect(page.locator('.proposal')).not.toContainText('prefer')
  await trigger.click()
  const panel = page.getByRole('dialog', { name: 'Настройки подборки' })
  await expect(panel).toBeVisible()
  await expect(panel.getByLabel('Жанр поиска')).toContainText('Хип-хоп')
  await panel.getByLabel('Насколько неожиданное').selectOption('close')
  await panel.getByRole('button', { name: 'Отмена', exact: true }).click()
  await expect(panel).not.toBeVisible()
  await expect(trigger).toBeFocused()
  expect(state.calls).toEqual([])
  expect(state.chat.filters).toEqual(initialFilters)
  expect(state.profile.filters).toEqual(initialFilters)
  await trigger.click()
  await expect(panel.getByLabel('Насколько неожиданное')).toHaveValue('explore')
  expect(state.unexpected).toEqual([])
})

test('mobile 375px filter dialog stays within viewport and Escape restores trigger focus', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const state = await fixture(page)
  const trigger = settingsButton(page)
  await expect(trigger).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await trigger.focus()
  await page.keyboard.press('Enter')
  const panel = page.getByRole('dialog', { name: 'Настройки подборки' })
  await expect(panel).toBeVisible()
  await expect.poll(() => panel.evaluate(element => element.contains(document.activeElement))).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await panel.getByLabel('Насколько неожиданное').selectOption('close')
  await page.keyboard.press('Escape')
  await expect(panel).not.toBeVisible()
  await expect(trigger).toBeFocused()
  expect(state.calls).toEqual([])
  expect(state.chat.filters).toEqual(initialFilters)
  await page.screenshot({ path: 'test-results/filter-ux-mobile-375.png', fullPage: true })
})
