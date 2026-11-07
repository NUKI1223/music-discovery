import { test, expect } from '@playwright/test'
import { requestRecommendations } from './helpers'

test('bounded history pages and editable chat memory survive reload and reset', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Нет аккаунта? Зарегистрироваться' }).click()
  await page.getByLabel('Логин', { exact: true }).fill(`memory_${Date.now()}`)
  await page.getByLabel('Пароль', { exact: true }).fill('memory-test-password')
  await page.getByLabel('Подтвердите пароль').fill('memory-test-password')
  await page.getByRole('button', { name: 'Создать аккаунт', exact: true }).click()
  await page.getByRole('button', { name: 'Сохранить вкус и продолжить' }).click()
  await requestRecommendations(page)
  await expect(page.locator('.release-card').first()).toBeVisible()
  const profileBefore = await page.evaluate(async () => (await fetch('/api/profile')).json())
  await page.evaluate(async () => {
    const session = await (await fetch('/api/auth/session')).json()
    const chat = (await (await fetch('/api/chats')).json())[0]
    for (let i = 0; i < 12; i++) {
      const response = await fetch(`/api/chats/${chat.id}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token }, body: JSON.stringify({ message_id: `history-${i}`, text: i === 0 ? 'Хочу спокойную музыку для работы' : 'Ещё варианты' }) })
      if (!response.ok) throw new Error(`History setup: ${response.status}`)
    }
  })
  await page.reload()
  await expect(page.locator('.message')).toHaveCount(20)
  await page.getByRole('button', { name: 'Развернуть панель чата' }).click()
  await page.getByRole('button', { name: 'Ранее', exact: true }).click()
  await expect(page.locator('.message')).toHaveCount(6)
  await expect(page.getByText('Хочу спокойную музыку для работы', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Позже', exact: true }).click()
  await expect(page.locator('.message')).toHaveCount(20)
  await page.getByRole('button', { name: 'Память диалога', exact: true }).click()
  await page.getByLabel('Цель и пожелания').fill('Сегодня без агрессивного звучания')
  await page.getByRole('button', { name: 'Сохранить пожелания', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Сохранить пожелания', exact: true })).toBeEnabled()
  await page.reload()
  await page.getByRole('button', { name: 'Память диалога', exact: true }).click()
  await expect(page.getByLabel('Цель и пожелания')).toHaveValue('Сегодня без агрессивного звучания')
  await page.getByRole('button', { name: 'Сбросить память диалога', exact: true }).click()
  await expect(page.getByLabel('Цель и пожелания')).toHaveValue('')
  expect(await page.evaluate(async () => (await fetch('/api/profile')).json())).toEqual(profileBefore)
  await page.reload()
  await expect(page.locator('.message')).toHaveCount(20)
  await expect(page.getByRole('button', { name: 'Ранее', exact: true })).toBeVisible()
})
