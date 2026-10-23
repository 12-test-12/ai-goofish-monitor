import { http } from '@/lib/http'

export interface AccountItem {
  name: string
  path: string
}

export interface AccountDetail extends AccountItem {
  content: string
}

export async function listAccounts(): Promise<AccountItem[]> {
  return await http('/api/accounts')
}

export async function getAccount(name: string): Promise<AccountDetail> {
  return await http(`/api/accounts/${encodeURIComponent(name)}`)
}

export async function createAccount(payload: { name: string; content: string }): Promise<AccountDetail> {
  return await http('/api/accounts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export async function updateAccount(name: string, content: string): Promise<AccountDetail> {
  return await http(`/api/accounts/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
}

export async function deleteAccount(name: string): Promise<{ message: string }> {
  return await http(`/api/accounts/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export interface AccountLoginSession {
  session_id: string
  name: string
  status: 'starting' | 'waiting' | 'scanned' | 'success' | 'expired' | 'failed' | 'cancelled'
  qr_image: string | null
  error: string | null
}

export async function startAccountLogin(name: string): Promise<AccountLoginSession> {
  return await http('/api/accounts/qr-login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

export async function getAccountLogin(sessionId: string): Promise<AccountLoginSession> {
  return await http(`/api/accounts/qr-login/${encodeURIComponent(sessionId)}`)
}

export async function cancelAccountLogin(sessionId: string): Promise<void> {
  await http(`/api/accounts/qr-login/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}
