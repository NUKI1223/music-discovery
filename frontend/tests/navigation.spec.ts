import { expect, test, type Page } from '@playwright/test'

const origin = 'http://127.0.0.1:8765'
async function account(page: Page) {
  const username = `nav_${crypto.randomUUID().slice(0, 12)}`
  const session = await (await page.request.post('/api/auth/register', { headers: { Origin: origin }, data: {
    username, password: 'navigation-test-password', password_confirmation: 'navigation-test-password',
  } })).json()
  const headers = { Origin: origin, 'X-CSRF-Token': session.csrf_token }
  const profile = await (await page.request.get('/api/profile')).json()
  await page.request.put('/api/profile', { headers, data: { ...profile, onboarded: true } })
  return { headers, username, userId: session.user.id }
}
async function createChat(page: Page, headers: Record<string, string>, title: string) {
  return (await page.request.post('/api/chats', { headers, data: { title, filters: { genre_scope_id: 'hip-hop' } } })).json()
}

test('reload restores the selected older chat, collection contents and profile independently in each tab', async ({ page, context }) => {
  const { headers } = await account(page)
  const older = await createChat(page, headers, 'Первый выбранный чат')
  const message = await (await page.request.post(`/api/chats/${older.id}/messages`, { headers, data: { message_id: crypto.randomUUID(), apply_filters: true } })).json()
  const card = message.cards[0]
  await page.request.post('/api/feedback', { headers, data: { feedback_id: crypto.randomUUID(), decision_id: card.decision_id, release_id: card.release_id, kind: 'save_for_later' } })
  await createChat(page, headers, 'Самый новый чат')
  await page.goto('/')
  await page.locator('.chat-history').getByRole('button', { name: older.title, exact: true }).click()
  await expect(page.locator('.screen-title')).toHaveText(older.title)
  await page.reload()
  await expect(page.locator('.screen-title')).toHaveText(older.title)
  await expect(page.getByRole('heading', { name: card.title, exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Моя коллекция', exact: true }).click()
  await expect(page.locator('.collection-entry')).toHaveCount(1)
  const otherTab = await context.newPage()
  try {
    await otherTab.goto('/')
    await expect(otherTab.locator('.conversation')).toBeVisible()
    await page.reload()
    await expect(page.locator('.collection-view')).toBeVisible()
    await expect(page.locator('.collection-entry')).toHaveCount(1)
    await expect(page.getByRole('heading', { name: card.title, exact: true })).toBeVisible()
    await otherTab.reload()
    await expect(otherTab.locator('.conversation')).toBeVisible()
    await page.getByRole('button', { name: 'Настройки вкуса', exact: true }).click()
    await page.reload()
    await expect(page.locator('.profile-view')).toBeVisible()
    await page.getByRole('button', { name: 'Отмена', exact: true }).click()
    await expect(page.locator('.screen-title')).toHaveText(older.title)
  } finally { await otherTab.close() }
})

test('saved navigation is account scoped and a deleted chat has a safe fallback', async ({ page }) => {
  const first = await account(page)
  const removed = await createChat(page, first.headers, 'Удаляемый диалог')
  await page.goto('/')
  await expect(page.locator('.screen-title')).toHaveText(removed.title)
  await page.request.delete(`/api/chats/${removed.id}`, { headers: first.headers })
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('Этот диалог больше недоступен')
  await expect(page.locator('.conversation')).toBeVisible()
  await page.reload()
  await expect(page.locator('.conversation')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.getByRole('button', { name: 'Моя коллекция', exact: true }).click()
  await expect(page.locator('.collection-view')).toBeVisible()
  await page.getByRole('button', { name: 'Выйти из аккаунта' }).click()
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible()
  await account(page)
  await page.goto('/')
  await expect(page.locator('.conversation')).toBeVisible()
  await expect(page.locator('.collection-view')).toHaveCount(0)
  await page.getByRole('button', { name: 'Выйти из аккаунта' }).click()
  await page.getByLabel('Логин', { exact: true }).fill(first.username)
  await page.getByLabel('Пароль', { exact: true }).fill('navigation-test-password')
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await expect(page.locator('.collection-view')).toBeVisible()
  await page.route('**/api/collection', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Коллекция временно недоступна"}' }))
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('Коллекция временно недоступна')
  await page.unroute('**/api/collection')
  await page.getByRole('button', { name: 'Повторить', exact: true }).click()
  await expect(page.locator('.collection-view')).toBeVisible()
})
