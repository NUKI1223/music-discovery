import { expect, test, type Page } from '@playwright/test'
import { requestRecommendations } from './helpers'
import AxeBuilder from '@axe-core/playwright'

async function account(page: Page, onboarded = true) {
  const origin = 'http://127.0.0.1:8765'
  const session = await (await page.request.post('/api/auth/register', { headers: { Origin: origin }, data: {
    username: `audit_${Date.now()}`, password: 'audit-test-password', password_confirmation: 'audit-test-password',
  } })).json()
  const headers = { Origin: origin, 'X-CSRF-Token': session.csrf_token }
  const profile = await (await page.request.get('/api/profile')).json()
  if (onboarded) await page.request.put('/api/profile', { headers, data: { ...profile, onboarded: true } })
  await page.goto('/')
  return { headers, profile }
}
async function accessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa', 'best-practice']).analyze()
  expect(results.violations.map(v => ({ id: v.id, targets: v.nodes.map(n => n.target) }))).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
}

test('browser username validation rejects invalid characters without regex errors', async ({ page }) => {
  const errors: string[] = []
  page.on('console', entry => { if (entry.type() === 'error' && /pattern|regex/i.test(entry.text())) errors.push(entry.text()) })
  await page.goto('/')
  await page.getByRole('button', { name: 'Нет аккаунта? Зарегистрироваться' }).click()
  const input = page.getByLabel('Логин', { exact: true })
  for (const name of ['invalid space', 'имя', 'name@']) {
    await input.fill(name)
    expect(await input.evaluate(node => (node as HTMLInputElement).checkValidity())).toBe(false)
  }
  for (const name of ['good-name', 'good_name', 'Name123']) {
    await input.fill(name)
    expect(await input.evaluate(node => (node as HTMLInputElement).checkValidity())).toBe(true)
  }
  expect(errors).toEqual([])
})

test('profile drafts survive genre roundtrip and cancelling starter preview does not save it', async ({ page }) => {
  await account(page)
  await page.getByRole('button', { name: 'Настройки вкуса' }).click()
  const original = await (await page.request.get('/api/profile')).json()
  await page.getByRole('button', { name: '+ [DEMO] Experimental hip-hop artist 1', exact: true }).click()
  await page.getByLabel('Жанр поиска').selectOption('jazz')
  await expect(page.getByRole('button', { name: 'Сохранить вкус и продолжить' })).toBeEnabled()
  await page.getByLabel('Жанр поиска').selectOption('hip-hop')
  await expect(page.getByRole('button', { name: 'Убрать [DEMO] Experimental hip-hop artist 1', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Использовать мой стартовый вкус' }).click()
  await expect(page.getByRole('button', { name: 'Сохранить вкус и продолжить' })).toBeEnabled()
  expect(await (await page.request.get('/api/profile')).json()).toEqual(original)
  await page.getByRole('button', { name: 'Отмена', exact: true }).click()
  await page.reload()
  expect(await (await page.request.get('/api/profile')).json()).toEqual(original)
})

test('navigation waits for collection and cannot send a message to a stale screen', async ({ page }) => {
  await account(page)
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/collection', async route => { await gate; await route.continue() })
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await expect(page.getByText('Загружаем коллекцию…', { exact: true })).toBeVisible()
  await expect(page.locator('.navigation-status .loading-spinner')).toHaveCSS('animation-name', 'loading-spin')
  await expect(page.locator('.navigation-progress')).toHaveCSS('animation-name', 'navigation-sweep')
  await page.screenshot({ path: 'test-results/navigation-loading.png' })
  await expect(page.getByRole('button', { name: 'Новый чат', exact: true })).toBeDisabled()
  await expect(page.getByLabel('Сообщение помощнику')).toBeDisabled()
  release()
  await expect(page.locator('.collection-view')).toBeVisible()
  await expect(page.locator('.navigation-status')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Настройки подборки' })).toHaveCount(0)
})

for (const width of [375, 768, 1440]) test(`all main screens have accessible landmarks and headings at ${width}px`, async ({ page }) => {
  test.setTimeout(90000)
  await page.setViewportSize({ width, height: 1000 })
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByRole('button', { name: 'Войти', exact: true }).waitFor()
  await accessible(page)
  await account(page, false)
  await page.getByRole('heading', { name: 'Что вам нравится?' }).waitFor()
  await accessible(page)
  await page.getByRole('button', { name: 'Сохранить вкус и продолжить' }).click()
  await page.getByRole('heading', { name: 'С чего начнём слушать?' }).waitFor()
  await accessible(page)
  await requestRecommendations(page)
  await page.locator('.release-card').first().waitFor()
  await accessible(page)
  await page.locator('.release-card').first().getByRole('button', { name: 'Уже знаю', exact: true }).click()
  await expect(page.locator('.release-card').first().getByRole('button', { name: 'Уже знаю', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: 'Настройки подборки' }).click()
  await accessible(page)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Настройки подборки' })).toBeFocused()
  if (width < 701) await page.getByRole('button', { name: 'Открыть меню' }).click()
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await page.locator('.collection-entry').first().waitFor()
  await accessible(page)
  const top = await page.locator('.release-card').first().evaluate(node => node.getBoundingClientRect().top)
  expect(top).toBeLessThan(width < 701 ? 560 : 520)
  await page.getByText('Фильтры и группировка', { exact: true }).click()
  await accessible(page)
  await page.getByRole('button', { name: 'Очистить коллекцию' }).click()
  await accessible(page)
  await page.getByLabel('Удалить из памяти').check()
  await accessible(page)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Очистить коллекцию' })).toBeFocused()
  if (width < 701) await page.getByRole('button', { name: 'Открыть меню' }).click()
  await page.getByRole('button', { name: 'Настройки вкуса' }).click()
  await accessible(page)
  expect(errors).toEqual([])
})


test('chat trash appears on row hover or keyboard focus and keeps deletion separate from navigation', async ({ page }) => {
  const { headers } = await account(page)
  for (const title of ['Первый диалог', 'Второй диалог', 'Очень длинное название диалога про музыку и новые альбомы без переполнения панели']) {
    await page.request.post('/api/chats', { headers, data: { title, filters: { genre_scope_id: 'hip-hop' } } })
  }
  await page.reload()
  const row = page.locator('.chat-history-row').filter({ hasText: 'Первый диалог' })
  const trash = row.getByRole('button', { name: 'Удалить чат «Первый диалог»', exact: true })
  await page.locator('.screen-title').hover()
  await expect(trash).toHaveCSS('opacity', '0')
  await expect(trash).toHaveCSS('pointer-events', 'none')
  const history = page.locator('.chat-history')
  expect(await history.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
  const titleButton = row.getByRole('button', { name: 'Первый диалог', exact: true })
  const rowBox = (await row.boundingBox())!
  const before = (await titleButton.boundingBox())!
  expect(before.width).toBeCloseTo(rowBox.width, 0)
  const title = await page.locator('.screen-title').innerText()
  await row.hover()
  await expect(trash).toHaveCSS('opacity', '1')
  expect(await history.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
  await trash.hover()
  const after = (await titleButton.boundingBox())!
  const trashBox = (await trash.boundingBox())!
  expect(after.width).toBeCloseTo(before.width, 0)
  expect(trashBox.x).toBeGreaterThan(after.x)
  expect(trashBox.x + trashBox.width).toBeLessThanOrEqual(after.x + after.width + 1)
  await expect(trash.locator('svg')).not.toHaveCSS('transform', 'none')
  await page.screenshot({ path: 'test-results/chat-trash-hover.png' })
  await trash.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.locator('.screen-title')).toHaveText(title)
  await page.keyboard.press('Escape')
  await expect(trash).toBeFocused()
  await page.locator('.screen-title').hover()
  await row.getByRole('button', { name: 'Первый диалог', exact: true }).focus()
  await page.keyboard.press('Tab')
  await expect(trash).toBeFocused()
  await expect(trash).toHaveCSS('opacity', '1')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await trash.hover()
  await expect(trash.locator('svg')).toHaveCSS('transform', 'none')
  await page.route('**/api/collection', async route => { await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Временно недоступно"}' }) })
  await page.getByRole('button', { name: 'Моя коллекция' }).click()
  await expect(page.getByRole('alert')).toContainText('Временно недоступно')
  await expect(page.locator('.navigation-status')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Новый чат', exact: true })).toBeEnabled()
})

test('touch devices expose chat deletion without hover and reduced motion keeps loading visible', async ({ browser }) => {
  const context = await browser.newContext({ baseURL: 'http://127.0.0.1:8765', viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true, reducedMotion: 'reduce' })
  const page = await context.newPage()
  try {
    await account(page)
    await page.getByRole('button', { name: 'Открыть меню' }).tap()
    await page.getByRole('button', { name: 'Новый чат', exact: true }).tap()
    await expect(page.locator('.chat-history-row')).toHaveCount(1)
    await page.getByRole('button', { name: 'Открыть меню' }).tap()
    const trash = page.getByRole('button', { name: 'Удалить чат «Новый чат»', exact: true })
    await expect(trash).toHaveCSS('opacity', '1')
    await expect(trash).toHaveCSS('pointer-events', 'auto')
    expect(await page.locator('.chat-history').evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
    await trash.tap()
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.getByRole('button', { name: 'Отмена', exact: true }).tap()
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    await page.route('**/api/collection', async route => { await gate; await route.continue() })
    await page.getByRole('button', { name: 'Моя коллекция' }).tap()
    await expect(page.locator('.navigation-status')).toBeVisible()
    await expect(page.locator('.sidebar')).not.toHaveClass(/is-open/)
    await expect(page.locator('.navigation-status .loading-spinner')).toHaveCSS('animation-name', 'none')
    await expect(page.locator('.navigation-progress')).toHaveCSS('animation-name', 'none')
    await page.screenshot({ path: 'test-results/navigation-loading-mobile.png' })
    release()
    await expect(page.locator('.collection-view')).toBeVisible()
  } finally { await context.close() }
})
