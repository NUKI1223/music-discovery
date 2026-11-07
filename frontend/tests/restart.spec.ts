import { expect, test } from '@playwright/test'
import { requestRecommendations } from './helpers'
import { spawn, type ChildProcess } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

for (const kind of ['Прослушал — нравится', 'Уже знаю']) {
test(`history, session and ${kind} survive restart and exclude the release in a new chat`, async ({ page }) => {
  test.setTimeout(60000)
  const directory = mkdtempSync(join(tmpdir(), 'discovery-restart-'))
  const address = 'http://127.0.0.1:8766'
  let server: ChildProcess | undefined
  async function start() {
    server = spawn('../.venv/bin/python', ['../scripts/web_test_server.py', '--data-dir', directory, '--port', '8766'], {
      env: { ...process.env, DISCOVERY_DOTENV: '0' }, stdio: ['ignore', 'ignore', 'pipe'],
    })
    let failure = ''
    server.stderr?.on('data', chunk => { failure = (failure + chunk.toString()).slice(-2000) })
    for (let attempt = 0; attempt < 150; attempt++) {
      if (server.exitCode !== null) throw new Error(`Test server failed: ${failure}`)
      try { if ((await fetch(`${address}/openapi.json`)).ok) return } catch { /* wait for bind */ }
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    throw new Error(`Test server did not start: ${failure}`)
  }
  async function stop() {
    const running = server
    if (!running || running.exitCode !== null) return
    const closed = new Promise<void>(resolve => running.once('exit', () => resolve()))
    running.kill('SIGTERM')
    await closed
    server = undefined
  }
  try {
    await start()
    await page.goto(address)
    await page.getByRole('button', { name: 'Нет аккаунта? Зарегистрироваться' }).click()
    await page.getByLabel('Логин', { exact: true }).fill('restart_account')
    await page.getByLabel('Пароль', { exact: true }).fill('restart-password-123')
    await page.getByLabel('Подтвердите пароль', { exact: true }).fill('restart-password-123')
    await page.getByRole('button', { name: 'Создать аккаунт', exact: true }).click()
    await page.getByRole('button', { name: 'Сохранить вкус и продолжить' }).click()
    await requestRecommendations(page)
    const card = page.locator('.release-card').first()
    await expect(card).toBeVisible()
    const title = await card.getByRole('heading').innerText()
    await card.getByRole('button', { name: kind, exact: true }).click()
    await expect(card.getByRole('button', { name: kind, exact: true })).toHaveAttribute('aria-pressed', 'true')
    await stop()
    await start()
    await page.reload()
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible()
    await expect(page.locator('.release-card').first().getByRole('button', { name: kind, exact: true })).toHaveAttribute('aria-pressed', 'true')
    await page.getByRole('button', { name: 'Моя коллекция' }).click()
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible()
    await page.getByText('Фильтры и группировка', { exact: true }).click()
    await page.getByRole('combobox', { name: 'Показать', exact: true }).selectOption('listened')
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible()
    await expect(page.getByText('В прослушанных · больше не рекомендуем', { exact: true })).toBeVisible()
    await page.screenshot({ path: `test-results/restart-${kind === 'Уже знаю' ? 'known' : 'liked'}.png`, fullPage: true })
    await page.getByRole('button', { name: 'Новый чат', exact: true }).click()
    await requestRecommendations(page)
    await expect(page.locator('.release-card').first()).toBeVisible()
    await expect(page.getByRole('heading', { name: title, exact: true })).toHaveCount(0)
  } finally {
    await stop()
  }
})

}
