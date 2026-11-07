import type { components } from './api.generated'
export type Schema = components['schemas']
export type Filters = Required<Schema['Filters']>
export type Profile = Required<Schema['Profile']> & { filters: Filters }
export type Chat = Schema['ChatDetail'] & { filters: Filters }
export type Card = Schema['ReleaseCard']
export const defaults: Filters = { genre_scope_id: 'hip-hop', mode: 'explore', obscurity: 'prefer', locked_subgenre_ids: [], exclude_known_artists: false, reference_artist_ids: [], requested_artist_ids: [], excluded_artist_ids: [], preferred_descriptors: [], avoided_descriptors: [] }
let csrf = ''
export const setCSRF = (token: string) => { csrf = token }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message) } }
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, { method, credentials: 'same-origin', headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(method === 'GET' ? {} : { 'X-CSRF-Token': csrf }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
  } catch { throw new ApiError('Нет связи с сервером. Проверьте подключение и повторите запрос.', 0) }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    const detail = typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос. Проверьте введённые данные.'
    if (response.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event('session-expired'))
    throw new ApiError(detail, response.status)
  }
  return response.json() as Promise<T>
}
export const messageOf = (error: unknown) => error instanceof Error ? error.message : 'Произошла ошибка. Попробуйте ещё раз.'
