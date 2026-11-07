import { expect, test } from '@playwright/test'

for (const maximum of [0, 1, 2]) test(`general popularity maximum ${maximum} survives genre, starter taste, save and reload`, async ({ page }) => {
  const filters = { genre_scope_id: 'hip-hop', mode: 'explore', obscurity: 'prefer', locked_subgenre_ids: [], exclude_known_artists: false, reference_artist_ids: [], excluded_artist_ids: [], requested_artist_ids: [], preferred_descriptors: [], avoided_descriptors: [] }
  const labels = {
    known_artist: { classification: 'widely_known', source: 'explicit_user', evidence: 'Я считаю этого исполнителя известным.', labelled_at: '2026-11-02T12:00:00Z' },
    discovery_artist: { classification: 'less_known', source: 'explicit_user', evidence: 'Для меня это менее известная находка.', labelled_at: '2026-11-02T12:00:00Z' },
  }
  let profile = { onboarded: true, filters, seed_artist_ids: [], avoid_tag_ids: [], request_only_artist_ids: ['request_artist'], limit_widely_known_artists: false, max_widely_known_artists: 2, artist_popularity_labels: labels }
  const writes: typeof profile[] = []
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); const path = url.pathname; const method = route.request().method()
    const respond = (json: unknown) => route.fulfill({ json })
    if (path === '/api/auth/session') return respond({ user: { id: 'policy-account', username: 'policy_listener' }, csrf_token: 'csrf' })
    if (path === '/api/chats') return respond([])
    if (path === '/api/catalog/genres') return respond([{ id: 'hip-hop', name: 'Hip-hop' }, { id: 'jazz', name: 'Jazz' }])
    if (path === '/api/catalog/artists') return respond([{ id: 'request_artist', name: 'Исполнитель по запросу' }])
    if (path === '/api/profile' && method === 'GET') return respond({ ...profile, filters: { ...profile.filters, genre_scope_id: url.searchParams.get('genre_scope_id') || profile.filters.genre_scope_id } })
    if (path === '/api/profile/starter') return respond({ ...profile, filters })
    if (path === '/api/profile' && method === 'PUT') { profile = route.request().postDataJSON(); writes.push(structuredClone(profile)); return respond(profile) }
    return route.fulfill({ status: 500, json: { detail: `Unexpected ${method} ${path}` } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Настройки вкуса', exact: true }).click()
  const limit = page.getByLabel('Учитывать известность исполнителей', { exact: true })
  await expect(limit).not.toBeChecked()
  const mode = page.getByLabel('Максимум широко известных в подборке')
  await expect(mode).toBeDisabled()
  await limit.check()
  await mode.selectOption(String(maximum))
  await page.getByLabel('Жанр поиска').selectOption('jazz')
  await expect(page.getByLabel('Жанр поиска')).toHaveValue('jazz')
  await expect(limit).toBeChecked()
  await expect(mode).toHaveValue(String(maximum))
  await page.getByRole('button', { name: 'Использовать мой стартовый вкус', exact: true }).click()
  await expect(page.getByLabel('Жанр поиска')).toHaveValue('hip-hop')
  await expect(limit).toBeChecked()
  await expect(mode).toHaveValue(String(maximum))
  await expect(page.getByText('Ваши метки: известных — 1, менее известных — 1.', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Сохранить вкус и продолжить', exact: true }).click()
  await expect.poll(() => writes.length).toBe(1)
  expect(writes[0].limit_widely_known_artists).toBe(true)
  expect(writes[0].max_widely_known_artists).toBe(maximum)
  expect(writes[0].artist_popularity_labels).toEqual(labels)
  expect(writes[0].request_only_artist_ids).toEqual(['request_artist'])
  await page.reload()
  await page.getByRole('button', { name: 'Настройки вкуса', exact: true }).click()
  await expect(limit).toBeChecked()
  await expect(mode).toHaveValue(String(maximum))
  await expect(page.getByText('Ваши метки: известных — 1, менее известных — 1.', { exact: true })).toBeVisible()
  await page.screenshot({ path: `test-results/popularity-policy-${maximum}-profile.png`, fullPage: true })
})
