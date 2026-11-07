import { test, expect } from '@playwright/test'
import { requestRecommendations } from './helpers'
import { albumLink, releaseMedia } from '../src/releaseMedia'
import type { Card } from '../src/api'

const mbid = '011a70f6-921f-484d-80e2-a708d4d7d918'
const base = { title: 'Album & Friends', artists: ['Artist / Name'], links: [] } as unknown as Card

test('exact artwork identities and streaming pages stay separate from search and unsafe links', () => {
  const media = releaseMedia({ ...base, links: [
    { label: 'MusicBrainz', url: `https://musicbrainz.org/release-group/${mbid}` },
    { label: 'Spotify', url: 'https://open.spotify.com/album/abc123' },
    { label: 'wrong host', url: `https://musicbrainz.org.evil.test/release-group/${mbid}` },
    { label: 'unsafe', url: 'javascript:alert(1)' },
    { label: 'credentials', url: 'https://name:secret@open.spotify.com/album/abc123' },
  ] })
  expect(media.artwork).toBe(`https://coverartarchive.org/release-group/${mbid}/front-500`)
  expect(media.listening).toEqual([{ label: 'Spotify', url: 'https://open.spotify.com/album/abc123' }])
  expect(media).not.toHaveProperty('searches')
  expect(releaseMedia(base).listening).toEqual([])
  expect(media.sources).toHaveLength(2)
  expect(releaseMedia(base).artwork).toBeUndefined()
  expect(releaseMedia({ ...base, links: [{ label: 'mb', url: 'https://musicbrainz.org/release-group/not-an-id' }] }).artwork).toBeUndefined()
})

for (const community of [false, true, 'aoty', 'slow-archive', 'deezer', 'vibe'] as const) test(`chat artwork and dark theme, community=${community}`, async ({ page }) => {
  await page.route('**/api/releases/*/artwork', route => route.fulfill({ json: community ? { artwork: 'https://coverartarchive.org/test-image', listening_service: community === 'deezer' ? 'Deezer' : 'Apple Music', listening_url: community === 'aoty' ? null : community === 'deezer' ? 'https://www.deezer.com/album/123' : 'https://music.apple.com/us/album/test/123', source_url: community === 'aoty' ? 'https://www.albumoftheyear.org/album/123-test.php' : null, aoty_status: community === 'aoty' ? 'found' : null } : { artwork: null, listening_url: null } }))
  await page.route('https://coverartarchive.org/test-image', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="500"><rect width="500" height="500" fill="green"/></svg>' }))
  await page.route('https://coverartarchive.org/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="500"><rect width="500" height="500" fill="#002fa7"/><text x="30" y="250" fill="white" font-size="40">Test artwork</text></svg>' }))
  await page.route(`https://coverartarchive.org/release-group/${mbid}/front-500`, async route => {
    if (community === 'slow-archive') { await new Promise(resolve => setTimeout(resolve, 10000)); await route.abort().catch(() => {}); return }
    await route.fallback()
  })
  await page.route('**/api/chats/**', async route => {
    const response = await route.fetch()
    const body = await response.json()
    const messages = body.messages ?? [body]
    for (const message of messages) if (message.cards?.length) {
      message.cards[0].links = [...(community === 'aoty' ? [{label: 'AOTY', url: 'https://www.albumoftheyear.org/album/123-test.php'}] : community && community !== 'slow-archive' ? [] : [{ label: 'MusicBrainz', url: `https://musicbrainz.org/release-group/${mbid}` }]), { label: 'Spotify', url: 'https://open.spotify.com/album/abc123' }]
      if (community === 'vibe') {
        message.cards[0].links.push({ label: 'AOTY', url: 'https://www.albumoftheyear.org/album/123-test.php' }, { label: 'AOTY Vibe', url: 'https://www.albumoftheyear.org/album/123-test.php' })
        message.cards[0].explanation = ['В описании AOTY Vibe совпадают пожелания: atmospheric.']
        message.cards[0].warnings = ['Описания источников субъективны и не являются измерением аудио.']
      }
    }
    await route.fulfill({ response, json: body })
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Нет аккаунта? Зарегистрироваться' }).click()
  await page.getByLabel('Логин', { exact: true }).fill(`artwork_${Date.now()}`)
  await page.getByLabel('Пароль', { exact: true }).fill('artwork-test-password')
  await page.getByLabel('Подтвердите пароль').fill('artwork-test-password')
  await page.getByRole('button', { name: 'Создать аккаунт', exact: true }).click()
  await page.getByRole('button', { name: 'Сохранить вкус и продолжить' }).click()
  await requestRecommendations(page)
  const card = page.locator('.release-card').first()
  await expect(card).toHaveCSS('background-color', 'rgb(28, 28, 30)')
  if (community === 'vibe') {
    await expect(card.getByText('В описании AOTY Vibe совпадают пожелания: atmospheric.', { exact: true })).toBeVisible()
    await expect(card.getByText('Описания источников субъективны и не являются измерением аудио.', { exact: true })).toBeVisible()
    await expect(card.getByRole('link', { name: 'AOTY Vibe', exact: true })).toHaveAttribute('href', 'https://www.albumoftheyear.org/album/123-test.php')
    await expect(card.getByRole('link', { name: 'AOTY', exact: true })).toHaveCount(0)
  }
  await expect(card).toHaveCSS('border-radius', '12px')
  if (community === 'aoty') await expect(card.getByRole('link', { name: /Открыть .* в AOTY/ })).toBeVisible()
  if (community === 'slow-archive') await expect(card.locator('img')).toHaveAttribute('src', 'https://coverartarchive.org/test-image', { timeout: 4000 })
  if (community === 'deezer') {
    await expect(card.getByRole('link', { name: 'Deezer', exact: true })).toHaveAttribute('href', 'https://www.deezer.com/album/123')
    await expect(card.getByRole('link', { name: 'Apple Music', exact: true })).toHaveCount(0)
  }
  if (community === true) await expect(card.getByRole('link', { name: 'Apple Music', exact: true })).toBeVisible()
  await expect(card.locator('.release-artwork img')).toBeVisible()
  await expect.poll(() => card.locator('img').evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0)
  await expect(card.getByRole('link', { name: 'Spotify', exact: true })).toHaveAttribute('href', 'https://open.spotify.com/album/abc123')
  await expect(card.getByText('Найти альбом', { exact: true })).toHaveCount(0)
  await expect(card.locator('a[href*="/search"]')).toHaveCount(0)
  await card.evaluate(element => element.scrollIntoView({ block: 'start', behavior: 'instant' }))
  await page.screenshot({ path: 'test-results/release-artwork-desktop.png' })
  await card.locator('img').dispatchEvent('error')
  await expect(card.getByText('Нет обложки')).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await card.evaluate(element => element.scrollIntoView({ block: 'start', behavior: 'instant' }))
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/release-artwork-mobile.png' })
})

for (const reverse of [false, true]) test(`source link dedup keeps Vibe provenance, reverse=${reverse}`, () => {
  const links = [{ label: 'AOTY', url: 'https://www.albumoftheyear.org/album/123-test.php' }, { label: 'AOTY Vibe', url: 'https://www.albumoftheyear.org/album/123-test.php' }]
  const media = releaseMedia({ ...base, links: reverse ? links.reverse() : links })
  expect(media.sources).toEqual([{ label: 'AOTY Vibe', url: 'https://www.albumoftheyear.org/album/123-test.php' }])
})

test('missing albums never manufacture search buttons or accept non-album platform URLs', () => {
  const indirect = [
    'https://open.spotify.com/search/Artist%20Album', 'https://open.spotify.com/artist/abc',
    'https://music.apple.com/us/artist/test/123', 'https://music.apple.com/us/album/test/123?i=456',
    'https://music.youtube.com/search?q=album', 'https://bandcamp.com/search?q=album',
    'https://www.deezer.com/artist/123', 'https://artist.bandcamp.com/track/track',
  ]
  for (const url of indirect) {
    expect(albumLink(url)).toBeUndefined()
    const media = releaseMedia({ ...base, links: [{ label: 'Spotify', url }] }, url)
    expect(media.listening).toEqual([])
    expect(media.sources).toEqual([])
  }
  expect(albumLink('https://www.deezer.com/en/album/123')?.label).toBe('Deezer')
  expect(albumLink('https://artist.bandcamp.com/album/release')?.label).toBe('Bandcamp')
  expect(releaseMedia(base, 'javascript:alert(1)').listening).toEqual([])
})

test('card without a matched platform page shows an honest empty state', async ({ page }) => {
  await page.route('**/api/releases/*/artwork', route => route.fulfill({ json: { artwork: null, listening_url: 'https://open.spotify.com/search/not-an-album', listening_service: 'Spotify' } }))
  await page.route('**/api/chats/**', async route => {
    const response = await route.fetch(), body = await response.json()
    for (const message of body.messages ?? [body]) for (const card of message.cards ?? []) {
      card.links = [{ label: 'AOTY', url: 'https://www.albumoftheyear.org/album/123-test.php' },
        { label: 'Spotify', url: 'https://open.spotify.com/search/absent-album' }]
    }
    await route.fulfill({ response, json: body })
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Нет аккаунта? Зарегистрироваться' }).click()
  await page.getByLabel('Логин', { exact: true }).fill(`noalbum_${Date.now()}`)
  await page.getByLabel('Пароль', { exact: true }).fill('noalbum-test-password')
  await page.getByLabel('Подтвердите пароль').fill('noalbum-test-password')
  await page.getByRole('button', { name: 'Создать аккаунт', exact: true }).click()
  await page.getByRole('button', { name: 'Сохранить вкус и продолжить' }).click()
  await requestRecommendations(page)
  const card = page.locator('.release-card').first()
  await expect(card.getByText('Прямая ссылка на площадку пока не найдена.')).toBeVisible()
  await expect(card.getByRole('link', { name: 'Spotify', exact: true })).toHaveCount(0)
  await expect(card.locator('a[href*="/search"]')).toHaveCount(0)
  await expect(card.getByRole('link', { name: 'AOTY', exact: true })).toHaveAttribute('href', 'https://www.albumoftheyear.org/album/123-test.php')
})

const platforms = [
  ['Spotify', 'https://open.spotify.com/album/abc123'],
  ['Apple Music', 'https://music.apple.com/us/album/123'],
  ['YouTube Music', 'https://music.youtube.com/playlist?list=OLAK5uy_123'],
  ['Deezer', 'https://www.deezer.com/album/123'],
  ['Bandcamp', 'https://artist.bandcamp.com/album/album'],
  ['Яндекс Музыка', 'https://music.yandex.kz/album/123'],
  ['TIDAL', 'https://listen.tidal.com/album/123'],
  ['Amazon Music', 'https://music.amazon.com/albums/ABC123'],
  ['Qobuz', 'https://www.qobuz.com/gb-en/album/title/abc123'],
  ['Pandora', 'https://www.pandora.com/AL:123'],
  ['Napster', 'https://play.napster.com/album/alb.123'],
  ['Boomplay', 'https://www.boomplay.com/albums/123'],
  ['Anghami', 'https://play.anghami.com/album/123'],
  ['Beatport', 'https://www.beatport.com/release/album/123'],
]

test('all discovered platforms merge with catalog and legacy links without duplicate services', () => {
  const media = releaseMedia({ ...base, links: [{ label: 'Spotify', url: platforms[0][1] }] }, platforms[3][1], [
    ...platforms.map(([, url]) => ({ url })), { url: 'https://open.spotify.com/album/anotherEdition' },
    { url: 'https://tidal.com/track/123' }, { url: 'https://music.amazon.com/albums/ABC123?trackAsin=track' },
    { url: 'https://music.youtube.com/playlist?list=userList' }, { url: 'https://play.anghami.com/song/123' },
    { url: 'https://www.qobuz.com/gb-en/artist/artist/123' },
  ])
  expect(media.listening).toEqual(platforms.map(([label, url]) => ({ label, url })))
})

test('multiple platform buttons survive reload and fit mobile, with visible lookup and partial failure state', async ({ page }) => {
  let finishLookup: () => void = () => {}
  const pending = new Promise<void>(resolve => { finishLookup = resolve })
  await page.route('**/api/releases/*/artwork', async route => {
    await pending
    await route.fulfill({ json: { artwork: null, listening_url: platforms[3][1], listening_service: 'Deezer',
      listening_links: platforms.map(([service, url]) => ({ service, url, source_url: 'https://musicbrainz.org/release-group/' + mbid })),
      link_status: { apple: 'unavailable', musicbrainz: 'found', album_link: 'found' } } })
  })
  const origin = 'http://127.0.0.1:8765'
  const account = await page.request.post('/api/auth/register', { headers: { Origin: origin }, data: {
    username: `links_${Date.now()}`, password: 'multiple-links-password', password_confirmation: 'multiple-links-password',
  } })
  expect(account.ok()).toBeTruthy()
  const headers = { Origin: origin, 'X-CSRF-Token': (await account.json()).csrf_token }
  const profile = await (await page.request.get('/api/profile')).json()
  expect((await page.request.put('/api/profile', { headers, data: { ...profile, onboarded: true } })).ok()).toBeTruthy()
  await page.goto('/')
  await requestRecommendations(page)
  const card = page.locator('.release-card').first()
  await expect(card.getByText('Ищем площадки с этим альбомом…')).toBeVisible()
  await expect(card.locator('.platform-spinner')).toHaveCSS('animation-name', 'platform-spin')
  finishLookup()
  for (const [name, url] of platforms) await expect(card.getByRole('link', { name, exact: true })).toHaveAttribute('href', url)
  await expect(card.getByText('Часть площадок пока не удалось проверить.')).toBeVisible()
  await page.reload()
  await expect(card.getByRole('link', { name: 'Spotify', exact: true })).toHaveAttribute('href', platforms[0][1])
  await page.setViewportSize({ width: 390, height: 844 })
  for (const [name] of platforms) await expect(card.getByRole('link', { name, exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await card.screenshot({ path: 'test-results/multiple-platforms-mobile.png' })
})
