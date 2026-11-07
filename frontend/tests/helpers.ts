import { expect, type Page } from '@playwright/test'

/** Получить подборку обычным сообщением в чат (помощник в тестах — мок). */
export async function requestRecommendations(page: Page, text = 'Подбери релизы') {
  const before = await page.locator('.release-card').count()
  await page.getByLabel('Сообщение помощнику').fill(text)
  await page.getByRole('button', { name: 'Отправить сообщение' }).click()
  await expect(page.locator('.release-card')).not.toHaveCount(before)
  await expect(page.locator('.release-card').first()).toBeVisible()
}
