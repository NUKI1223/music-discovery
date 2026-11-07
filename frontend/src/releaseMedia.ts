import type { Card } from './api'

export function safeLink(value: string): string | undefined {
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password
      && !/[\u0000-\u001f]/.test(value) ? url.href : undefined
  } catch { return undefined }
}

function platform(host: string): boolean {
  return ['open.spotify.com', 'music.apple.com', 'itunes.apple.com', 'www.deezer.com', 'deezer.com',
    'www.youtube.com', 'youtube.com', 'music.youtube.com', 'youtu.be', 'bandcamp.com',
    'music.yandex.ru', 'music.yandex.kz', 'music.yandex.com', 'tidal.com', 'www.tidal.com', 'listen.tidal.com',
    'music.amazon.com', 'music.amazon.co.uk', 'music.amazon.de', 'music.amazon.co.jp',
    'qobuz.com', 'www.qobuz.com', 'play.qobuz.com', 'open.qobuz.com', 'pandora.com', 'www.pandora.com',
    'play.napster.com', 'boomplay.com', 'www.boomplay.com', 'play.anghami.com', 'beatport.com', 'www.beatport.com'].includes(host) || host.endsWith('.bandcamp.com')
}

// Only album pages supplied by the catalog or an identity-matched store lookup.
// A search/artist/track URL must not become a listening button or a source button.
export function albumLink(value: string): { label: string, url: string } | undefined {
  const href = safeLink(value)
  if (!href) return
  const url = new URL(href), host = url.hostname.toLowerCase()
  if (url.protocol !== 'https:' || url.port || url.hash) return
  let service: string | undefined
  if (host === 'open.spotify.com' && /^\/(?:intl-[a-z]+\/)?album\/[a-zA-Z0-9]+\/?$/.test(url.pathname)) service = 'Spotify'
  if (['music.apple.com', 'itunes.apple.com'].includes(host) && /^\/[a-z]{2}\/album\/(?:[^/]+\/)?(?:id)?\d+\/?$/.test(url.pathname) && !url.searchParams.has('i')) service = 'Apple Music'
  if (['www.deezer.com', 'deezer.com'].includes(host) && /^\/(?:[a-z]{2}\/)?album\/\d+\/?$/.test(url.pathname)) service = 'Deezer'
  if (['www.youtube.com', 'youtube.com', 'music.youtube.com'].includes(host) && url.pathname === '/playlist' && /^OLAK5uy_[\w-]+$/.test(url.searchParams.get('list') || '') && !url.searchParams.has('v')) service = 'YouTube Music'
  if (host.endsWith('.bandcamp.com') && /^\/album\/[^/]+\/?$/.test(url.pathname)) service = 'Bandcamp'
  if (['music.yandex.ru', 'music.yandex.kz', 'music.yandex.com'].includes(host) && /^\/album\/\d+\/?$/.test(url.pathname)) service = 'Яндекс Музыка'
  if (['tidal.com', 'www.tidal.com', 'listen.tidal.com'].includes(host) && /^\/(?:browse\/)?album\/\d+\/?$/.test(url.pathname)) service = 'TIDAL'
  if (['music.amazon.com', 'music.amazon.co.uk', 'music.amazon.de', 'music.amazon.co.jp'].includes(host) && /^\/albums\/[a-zA-Z0-9]+\/?$/.test(url.pathname) && !url.searchParams.has('trackAsin') && !url.searchParams.has('track')) service = 'Amazon Music'
  if ((['www.qobuz.com', 'qobuz.com'].includes(host) && /^\/[a-z]{2}-[a-z]{2}\/album\/[^/]+\/[a-zA-Z0-9]+\/?$/.test(url.pathname)) || (['play.qobuz.com', 'open.qobuz.com'].includes(host) && /^\/album\/[a-zA-Z0-9]+\/?$/.test(url.pathname))) service = 'Qobuz'
  if (['www.pandora.com', 'pandora.com'].includes(host) && /^\/AL:\d+\/?$/.test(url.pathname)) service = 'Pandora'
  if (host === 'play.napster.com' && /^\/album\/alb\.\d+\/?$/.test(url.pathname)) service = 'Napster'
  if (['www.boomplay.com', 'boomplay.com'].includes(host) && /^\/albums\/\d+\/?$/.test(url.pathname)) service = 'Boomplay'
  if (host === 'play.anghami.com' && /^\/album\/\d+\/?$/.test(url.pathname)) service = 'Anghami'
  if (['www.beatport.com', 'beatport.com'].includes(host) && /^\/release\/[^/]+\/\d+\/?$/.test(url.pathname)) service = 'Beatport'
  return service ? { label: service, url: href } : undefined
}

export function releaseMedia(card: Card, resolvedUrl?: string | null, resolvedLinks: { url: string }[] = []) {
  let artwork: string | undefined
  const listening: { label: string, url: string }[] = []
  const sources: { label: string, url: string }[] = []
  for (const link of card.links) {
    const href = safeLink(link.url)
    if (!href) continue
    const url = new URL(href)
    const host = url.hostname.toLowerCase()
    if (['musicbrainz.org', 'www.musicbrainz.org'].includes(host)) {
      const match = url.pathname.match(/^\/(release-group|release)\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\/?$/i)
      if (match && !artwork) artwork = `https://coverartarchive.org/${match[1]}/${match[2]}/front-500`
    }
    const direct = albumLink(href)
    if (platform(host) && !direct) continue
    const target = direct ? listening : sources
    const existing = target.find(item => item.url === href)
    if (!existing && (!direct || !listening.some(item => item.label === direct.label))) target.push(direct ?? { label: link.label, url: href })
    else if (existing && !direct && /\b(?:Vibe|Descriptors)\b/i.test(link.label) && !/\b(?:Vibe|Descriptors)\b/i.test(existing.label)) existing.label = link.label
  }
  for (const item of [...resolvedLinks, ...(resolvedUrl ? [{ url: resolvedUrl }] : [])]) {
    const resolved = albumLink(item.url)
    if (resolved && !listening.some(link => link.label === resolved.label)) listening.push(resolved)
  }
  const order = ['Spotify', 'Apple Music', 'YouTube Music', 'Deezer', 'Bandcamp', 'Яндекс Музыка', 'TIDAL', 'Amazon Music', 'Qobuz', 'Pandora', 'Napster', 'Boomplay', 'Anghami', 'Beatport']
  listening.sort((a, b) => order.indexOf(a.label) - order.indexOf(b.label))
  return { artwork, listening, sources }
}
