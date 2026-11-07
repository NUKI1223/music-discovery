import { useEffect, useRef, useState, type ReactNode } from 'react'
import { messageOf } from './api'

export function ConfirmDialog({ title, description, confirmLabel, onClose, onConfirm, children }: {
  title: string, description: string, confirmLabel: string,
  onClose: () => void, onConfirm: () => Promise<void>,
  children?: ReactNode,
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const lock = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const opener = document.activeElement as HTMLElement
    const element = dialog.current
    element?.showModal()
    return () => {
      element?.close()
      if (opener?.isConnected) opener.focus()
      else document.querySelector<HTMLElement>('.collection-search input, .composer textarea, .new-chat')?.focus()
    }
  }, [])
  async function confirm() {
    if (lock.current) return
    lock.current = true; setBusy(true); setError('')
    try { await onConfirm(); onClose() }
    catch (error) { setError(messageOf(error)) }
    finally { lock.current = false; setBusy(false) }
  }
  return <dialog ref={dialog} className="settings-dialog confirm-dialog" aria-labelledby="confirm-title" aria-describedby="confirm-description" onCancel={event => { event.preventDefault(); if (!lock.current) onClose() }}>
    <h2 id="confirm-title">{title}</h2><p id="confirm-description">{description}</p>
    <fieldset disabled={busy}>{children}</fieldset>
    {error && <p className="error" role="alert">{error}</p>}
    <footer className="dialog-actions"><button autoFocus disabled={busy} onClick={onClose}>Отмена</button><button className="danger-button" disabled={busy} onClick={confirm}>{busy ? 'Удаляем…' : confirmLabel}</button></footer>
  </dialog>
}
