import { expect, test, type Page } from '@playwright/test'
import { initialCollectionFilters, selectCollection } from '../src/Collection'
import type { Card } from '../src/api'

async function expectUniqueReleases(page: Page, count: number) {
  await expect.poll(() => page.locator('.collection-entry').evaluateAll(nodes => new Set(nodes.map(node => node.getAttribute('data-release-id'))).size)).toBe(count)
}

async function setup(page: Page) {
  const origin = 'http://127.0.0.1:8765'
  const account = await page.request.post('/api/auth/register', { headers: { Origin: origin }, data: {
    username: `lib_${Date.now()}`, password: 'test-library-password', password_confirmation: 'test-library-password',
  } })
  expect(account.ok()).toBeTruthy()
  const headers = { Origin: origin, 'X-CSRF-Token': (await account.json()).csrf_token }
  const profile = await (await page.request.get('/api/profile')).json()
  await page.request.put('/api/profile', { headers, data: { ...profile, onboarded: true } })
  const sets: { id: string, title: string, cards: Card[] }[] = []
  for (const [title, genre] of [['Хип-хоп на вечер', 'hip-hop'], ['Джаз утром: спокойные альбомы для долгих прогулок и вечеров, когда хочется открыть новых исполнителей', 'jazz']]) {
    const chat = await (await page.request.post('/api/chats', { headers, data: { title, filters: { genre_scope_id: genre } } })).json()
    const response = await page.request.post(`/api/chats/${chat.id}/messages`, { headers, data: { message_id: `library_${Date.now()}_${sets.length}`, apply_filters: true } })
    const cards = (await response.json()).cards as Card[]
    expect(cards.length).toBe(5)
    for (const [index, card] of cards.slice(0, 2).entries()) {
      const saved = await page.request.post('/api/feedback', { headers, data: { feedback_id: `mark_${Date.now()}_${sets.length}_${index}`, decision_id: card.decision_id, release_id: card.release_id, kind: index ? 'save_for_later' : 'listened_like' } })
      expect(saved.ok()).toBeTruthy()
    }
    sets.push({ id: chat.id, title, cards })
  }
  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Моя коллекция' })).toBeVisible()
  return { sets, headers }
}

const base: Card = { release_id: 'one', decision_id: 'd', title: 'Alpha', artists: ['Артём'], genres: ['Jazz'], year: 2020, explanation: [], warnings: [], links: [], historical_ratings: [], feedback: 'known', listened: true, chats: [{ id: 'chat-1', title: 'Джаз', deleted: false }], saved_at: '2026-01-01' }
test('collection filtering intersects search, chat, genre, status and independent source ratings', () => {
  const cards: Card[] = [base,
    { ...base, release_id: 'two', title: 'Beta', genres: ['Rock'], historical_ratings: [{ source: 'AOTY', rating: 90, scale: 100, count: 10 }, { source: 'RYM', rating: 2, scale: 5, count: 20 }] },
    { ...base, release_id: 'three', title: 'Gamma', year: null, historical_ratings: [{ source: 'AOTY', rating: 8, scale: 10, count: 5 }, { source: 'RYM', rating: 4.2, scale: 5, count: 30 }], feedback: 'save_for_later', listened: false, chats: [], saved_at: '2026-11-01' }]
  const ids = (filters: Partial<typeof initialCollectionFilters>) => selectCollection(cards, { ...initialCollectionFilters, ...filters }).map(c => c.release_id)
  expect(ids({ query: 'АРТЕМ', chat: 'chat-1', genre: 'Jazz', status: 'listened' })).toEqual(['one'])
  expect(ids({ sort: 'aoty' })).toEqual(['two', 'three', 'one'])
  expect(ids({ sort: 'rym' })).toEqual(['three', 'two', 'one'])
  expect(ids({ aoty: '0' })).toEqual(['three', 'two']) // missing is not zero
  expect(ids({ aoty: '80', rym: '4' })).toEqual(['three'])
  expect(ids({ chat: 'unlinked' })).toEqual(['three'])
  expect(ids({ query: 'unknown' })).toEqual([])
  expect(ids({ sort: 'year' })).toEqual(['one', 'two', 'three'])
  expect(ids({ sort: 'title' })).toEqual(['one', 'two', 'three'])
})

test('search, combined filters, chat grouping and navigation work on real collection', async ({ page }) => {
  const { sets } = await setup(page)
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await expectUniqueReleases(page, 4)
  await expect(page.locator('.collection-group-title')).toHaveCount(2)
  await expect(page.locator('.collection-origins')).toHaveCount(0)
  await expect(page.locator('.collection-group-title').getByRole('button', { name: sets[0].title, exact: true })).toHaveCount(1)
  const group = page.locator('.collection-group').filter({ has: page.getByRole('heading', { name: sets[1].title, exact: true }) })
  const heading = (await group.locator('h2').boundingBox())!
  const grid = (await group.locator('.collection-grid').boundingBox())!
  expect(heading.width).toBeCloseTo(grid.width, 0)
  expect(heading.y + heading.height).toBeLessThanOrEqual(grid.y)
  await page.screenshot({ path: 'test-results/library-desktop.png', fullPage: true })
  await page.getByLabel('Поиск по коллекции').fill(sets[0].cards[0].title)
  await expectUniqueReleases(page, 1)
  await page.getByLabel('Поиск по коллекции').fill('не существующий альбом')
  await expect(page.getByRole('heading', { name: 'Ничего не найдено' })).toBeVisible()
  await page.getByRole('button', { name: 'Показать всю коллекцию' }).click()
  await page.getByText('Фильтры и группировка', { exact: true }).click()
  await page.getByLabel('Чат', { exact: true }).selectOption(sets[1].id)
  const collection = await (await page.request.get('/api/collection')).json() as Card[]
  await expectUniqueReleases(page, collection.filter(card => card.chats?.some(chat => chat.id === sets[1].id)).length)
  await page.getByLabel('Показать', { exact: true }).selectOption('listened_like')
  await expectUniqueReleases(page, 1)
  const genre = sets[1].cards[0].genres[0]
  await page.getByLabel('Жанр', { exact: true }).selectOption(genre)
  await expectUniqueReleases(page, 1)
  await page.getByLabel('Группировать').selectOption('chat')
  await expect(page.locator('.collection-group h2')).toHaveText([sets[1].title])
  await page.getByLabel('AOTY от (из 100)').fill('100')
  await expect(page.getByRole('heading', { name: 'Ничего не найдено' })).toBeVisible()
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click()
  await expectUniqueReleases(page, 4)
  await page.getByLabel('Группировать').selectOption('chat')
  await expect(page.locator('.collection-group h2')).toHaveCount(2)
  await page.locator('.collection-group-title').getByRole('button', { name: sets[0].title }).click()
  await expect(page.locator('.topbar strong')).toHaveText(sets[0].title)
})

test('chat deletion confirms, retains albums and deletes the active last chat cleanly', async ({ page }) => {
  const { sets } = await setup(page)
  await page.locator('.chat-history-row').filter({ hasText: sets[0].title }).hover()
  await page.getByRole('button', { name: `Удалить чат «${sets[0].title}»`, exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Отмена', exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('.chat-history-row')).toHaveCount(2)
  await page.locator('.chat-history-row').filter({ hasText: sets[0].title }).hover()
  await page.getByRole('button', { name: `Удалить чат «${sets[0].title}»`, exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить чат', exact: true }).click()
  await expect(page.locator('.chat-history-row')).toHaveCount(1)
  await expect(page.locator('.topbar strong')).toHaveText(sets[1].title)
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await expectUniqueReleases(page, 4)
  await expect(page.locator('.collection-group-title').filter({ hasText: 'Удалённый чат' })).toHaveCount(1)
  await expect(page.locator('.collection-origins')).toHaveCount(0)
  await page.getByRole('button', { name: sets[1].title, exact: true }).first().click()
  await page.locator('.chat-history-row').filter({ hasText: sets[1].title }).hover()
  await page.getByRole('button', { name: `Удалить чат «${sets[1].title}»`, exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить чат', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'С чего начнём слушать?' })).toBeVisible()
  await page.reload()
  await expect(page.locator('.chat-history-row')).toHaveCount(0)
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await expectUniqueReleases(page, 4)
})

test('individual hide keeps marks, clear with forget resets only remaining albums, survives reload', async ({ page }) => {
  const { sets } = await setup(page)
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await page.getByLabel('Поиск по коллекции').fill(sets[0].cards[0].title)
  await page.getByRole('button', { name: 'Убрать из коллекции', exact: true }).first().click()
  await expect(page.getByLabel('Удалить из памяти')).not.toBeChecked()
  await page.getByRole('dialog').getByRole('button', { name: 'Убрать из коллекции', exact: true }).click()
  await expectUniqueReleases(page, 0)
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click()
  await expectUniqueReleases(page, 3)
  await page.getByLabel('Поиск по коллекции').fill(sets[1].cards[0].title)
  await page.getByRole('button', { name: 'Очистить коллекцию' }).click()
  await expect(page.getByRole('dialog')).toContainText('все 3 альбомов')
  await page.getByLabel('Удалить из памяти').check()
  await page.getByRole('button', { name: 'Удалить и забыть', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Здесь будет ваша музыка' })).toBeVisible()
  await page.getByRole('button', { name: sets[0].title, exact: true }).click()
  const hidden = page.locator('.release-card').filter({ has: page.getByRole('heading', { name: sets[0].cards[0].title, exact: true }) })
  await expect(hidden.getByRole('button', { name: 'Прослушал — нравится', exact: true })).toHaveAttribute('aria-pressed', 'true')
  const forgotten = page.locator('.release-card').filter({ has: page.getByRole('heading', { name: sets[0].cards[1].title, exact: true }) })
  await expect(forgotten.getByRole('button', { name: 'Послушать позже', exact: true })).toHaveAttribute('aria-pressed', 'false')
  await page.reload()
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await expectUniqueReleases(page, 0)
})

test('mobile library has no overflow and a failed delete keeps data and allows retry', async ({ page }) => {
  await setup(page)
  await page.setViewportSize({ width: 320, height: 740 })
  await page.getByRole('button', { name: 'Открыть меню' }).click()
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await expectUniqueReleases(page, 4)
  expect(await page.locator('.collection-view').evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/library-mobile-with-albums.png', fullPage: true })
  await page.getByText('Фильтры и группировка', { exact: true }).click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  await page.getByRole('button', { name: 'Очистить коллекцию' }).click()
  await page.route('**/api/collection?forget=false', async route => {
    if (route.request().method() === 'DELETE') await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Сервер временно недоступен.' }) })
    else await route.continue()
  })
  await page.getByRole('dialog').getByRole('button', { name: 'Убрать из коллекции', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Сервер временно недоступен.')
  await expectUniqueReleases(page, 4)
  await page.unroute('**/api/collection?forget=false')
  await page.getByRole('dialog').getByRole('button', { name: 'Убрать из коллекции', exact: true }).click()
  await expectUniqueReleases(page, 0)
  await page.screenshot({ path: 'test-results/library-mobile.png', fullPage: true })
})
