<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { listAccounts, getAccount, createAccount, updateAccount, deleteAccount, type AccountItem } from '@/api/accounts'
import { startAccountLogin, getAccountLogin, cancelAccountLogin, type AccountLoginSession } from '@/api/accounts'
import { QrCode, LoaderCircle } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { toast } from '@/components/ui/toast'
const { t } = useI18n()

const accounts = ref<AccountItem[]>([])
const isLoading = ref(false)
const isSaving = ref(false)
const router = useRouter()

const isCreateDialogOpen = ref(false)
const isEditDialogOpen = ref(false)
const isDeleteDialogOpen = ref(false)

const newName = ref('')
const newContent = ref('')
const editName = ref('')
const editContent = ref('')
const deleteName = ref('')
const createMode = ref<'qr' | 'json'>('qr')
const qrSession = ref<AccountLoginSession | null>(null)
const qrError = ref('')
const isQrStarting = ref(false)
const qrActive = computed(() => isQrStarting.value || ['starting', 'waiting', 'scanned'].includes(qrSession.value?.status ?? ''))
let qrTimer: ReturnType<typeof setTimeout> | undefined
let qrVersion = 0

async function stopQrLogin() {
  ++qrVersion
  clearTimeout(qrTimer)
  const session = qrSession.value
  qrSession.value = null
  isQrStarting.value = false
  if (session) await cancelAccountLogin(session.session_id).catch(() => {})
}

async function pollQrLogin(sessionId: string, version: number) {
  try {
    const session = await getAccountLogin(sessionId)
    if (version !== qrVersion) return
    qrSession.value = session
    if (session.status === 'success') {
      toast({ title: t('accounts.toasts.created'), description: t('accounts.qr.saved') })
      isCreateDialogOpen.value = false
      await fetchAccounts()
    } else if (['starting', 'waiting', 'scanned'].includes(session.status)) {
      qrTimer = setTimeout(() => void pollQrLogin(sessionId, version), 1200)
    }
  } catch (e) {
    if (version !== qrVersion) return
    qrError.value = (e as Error).message
    await stopQrLogin()
  }
}

async function handleStartQrLogin() {
  if (!/^[a-zA-Z0-9_-]{1,50}$/.test(newName.value.trim())) {
    qrError.value = t('accounts.qr.invalidName')
    return
  }
  await stopQrLogin()
  const version = ++qrVersion
  isQrStarting.value = true
  qrError.value = ''
  try {
    const session = await startAccountLogin(newName.value.trim())
    if (version !== qrVersion) {
      await cancelAccountLogin(session.session_id)
      return
    }
    qrSession.value = session
    void pollQrLogin(session.session_id, version)
  } catch (e) {
    if (version === qrVersion) qrError.value = (e as Error).message
  } finally {
    if (version === qrVersion) isQrStarting.value = false
  }
}

watch([isCreateDialogOpen, createMode], ([open, mode]) => {
  if (!open || mode !== 'qr') void stopQrLogin()
})
onBeforeUnmount(() => void stopQrLogin())

async function fetchAccounts() {
  isLoading.value = true
  try {
    accounts.value = await listAccounts()
  } catch (e) {
    toast({ title: t('accounts.toasts.loadFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    isLoading.value = false
  }
}

function openCreateDialog() {
  newName.value = ''
  newContent.value = ''
  createMode.value = 'qr'
  qrError.value = ''
  isCreateDialogOpen.value = true
}

async function openEditDialog(name: string) {
  isSaving.value = true
  try {
    const detail = await getAccount(name)
    editName.value = detail.name
    editContent.value = detail.content
    isEditDialogOpen.value = true
  } catch (e) {
    toast({ title: t('accounts.toasts.loadContentFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    isSaving.value = false
  }
}

function openDeleteDialog(name: string) {
  deleteName.value = name
  isDeleteDialogOpen.value = true
}

function goCreateTask(name: string) {
  router.push({ path: '/tasks', query: { account: name, create: '1' } })
}

async function handleCreateAccount() {
  if (!newName.value.trim() || !newContent.value.trim()) {
    toast({ title: t('accounts.toasts.incomplete'), description: t('accounts.toasts.createDescriptionRequired'), variant: 'destructive' })
    return
  }
  isSaving.value = true
  try {
    await createAccount({ name: newName.value.trim(), content: newContent.value.trim() })
    toast({ title: t('accounts.toasts.created') })
    isCreateDialogOpen.value = false
    await fetchAccounts()
  } catch (e) {
    toast({ title: t('accounts.toasts.createFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    isSaving.value = false
  }
}

async function handleUpdateAccount() {
  if (!editContent.value.trim()) {
    toast({ title: t('accounts.toasts.contentRequired'), description: t('accounts.toasts.updateDescriptionRequired'), variant: 'destructive' })
    return
  }
  isSaving.value = true
  try {
    await updateAccount(editName.value, editContent.value.trim())
    toast({ title: t('accounts.toasts.updated') })
    isEditDialogOpen.value = false
    await fetchAccounts()
  } catch (e) {
    toast({ title: t('accounts.toasts.updateFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    isSaving.value = false
  }
}

async function handleDeleteAccount() {
  isSaving.value = true
  try {
    await deleteAccount(deleteName.value)
    toast({ title: t('accounts.toasts.deleted') })
    isDeleteDialogOpen.value = false
    await fetchAccounts()
  } catch (e) {
    toast({ title: t('accounts.toasts.deleteFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    isSaving.value = false
  }
}

onMounted(fetchAccounts)
</script>

<template>
  <div>
    <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-2xl font-bold text-gray-800">{{ t('accounts.title') }}</h1>
        <p class="text-sm text-gray-500 mt-1">{{ t('accounts.description') }}</p>
      </div>
      <Button class="w-full sm:w-auto" @click="openCreateDialog">{{ t('accounts.add') }}</Button>
    </div>

    <Card class="app-surface mb-6 border-none">
      <CardHeader>
        <CardTitle>{{ t('accounts.qr.guideTitle') }}</CardTitle>
      </CardHeader>
      <CardContent class="text-sm text-gray-600 space-y-2">
        <p>{{ t('accounts.qr.guide') }}</p>
        <details>
          <summary class="cursor-pointer text-blue-600">{{ t('accounts.qr.manualGuide') }}</summary>
          <ol class="mt-3 list-decimal list-inside space-y-1">
            <li>{{ t('accounts.cookieGuide.step1Prefix') }} <a class="text-blue-600 hover:underline" href="https://chromewebstore.google.com/detail/xianyu-login-state-extrac/eidlpfjiodpigmfcahkmlenhppfklcoa" target="_blank" rel="noopener noreferrer">{{ t('accounts.cookieGuide.extension') }}</a></li>
            <li>{{ t('accounts.cookieGuide.step2Prefix') }} <a class="text-blue-600 hover:underline" href="https://www.goofish.com" target="_blank" rel="noopener noreferrer">{{ t('accounts.cookieGuide.website') }}</a></li>
            <li>{{ t('accounts.cookieGuide.step3') }}</li>
            <li>{{ t('accounts.cookieGuide.step4') }}</li>
            <li>{{ t('accounts.cookieGuide.step5') }}</li>
          </ol>
        </details>
      </CardContent>
    </Card>

    <Card class="app-surface border-none">
      <CardHeader>
        <CardTitle>{{ t('accounts.list.title') }}</CardTitle>
        <CardDescription>{{ t('accounts.list.description') }}</CardDescription>
      </CardHeader>
      <CardContent>
        <div class="space-y-4 md:hidden">
          <div v-if="isLoading" class="py-10 text-center text-sm text-muted-foreground">{{ t('common.loading') }}</div>
          <div v-else-if="accounts.length === 0" class="py-10 text-center text-sm text-muted-foreground">{{ t('accounts.list.empty') }}</div>
          <article
            v-else
            v-for="account in accounts"
            :key="account.name"
            class="app-surface-subtle p-4"
          >
            <div class="space-y-2">
              <div class="flex items-center justify-between gap-3">
                <h3 class="truncate text-base font-semibold text-slate-900">{{ account.name }}</h3>
                <Button size="sm" variant="outline" @click="goCreateTask(account.name)">{{ t('accounts.list.createTask') }}</Button>
              </div>
              <p class="break-all text-sm text-slate-500">{{ account.path }}</p>
            </div>
            <div class="mt-4 flex flex-wrap gap-2">
              <Button size="sm" variant="outline" class="flex-1 min-w-[120px]" @click="openEditDialog(account.name)">{{ t('accounts.list.update') }}</Button>
              <Button size="sm" variant="destructive" class="flex-1 min-w-[120px]" @click="openDeleteDialog(account.name)">{{ t('accounts.list.delete') }}</Button>
            </div>
          </article>
        </div>

        <div class="hidden md:block">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{{ t('accounts.list.name') }}</TableHead>
                <TableHead>{{ t('accounts.list.file') }}</TableHead>
                <TableHead class="text-right">{{ t('accounts.list.actions') }}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-if="isLoading">
                <TableCell colspan="3" class="h-20 text-center text-muted-foreground">{{ t('common.loading') }}</TableCell>
              </TableRow>
              <TableRow v-else-if="accounts.length === 0">
                <TableCell colspan="3" class="h-20 text-center text-muted-foreground">{{ t('accounts.list.empty') }}</TableCell>
              </TableRow>
              <TableRow v-else v-for="account in accounts" :key="account.name">
                <TableCell class="font-medium">{{ account.name }}</TableCell>
                <TableCell class="text-sm text-gray-500">{{ account.path }}</TableCell>
                <TableCell class="text-right">
                  <div class="flex justify-end gap-2">
                    <Button size="sm" variant="outline" @click="goCreateTask(account.name)">{{ t('accounts.list.createTask') }}</Button>
                    <Button size="sm" variant="outline" @click="openEditDialog(account.name)">{{ t('accounts.list.update') }}</Button>
                    <Button size="sm" variant="destructive" @click="openDeleteDialog(account.name)">{{ t('accounts.list.delete') }}</Button>
                  </div>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>

    <Dialog v-model:open="isCreateDialogOpen">
      <DialogContent class="max-h-[90dvh] w-[calc(100%-2rem)] overflow-y-auto sm:max-w-[700px]">
        <DialogHeader>
          <DialogTitle>{{ t('accounts.createDialog.title') }}</DialogTitle>
          <DialogDescription>{{ t(createMode === 'qr' ? 'accounts.qr.description' : 'accounts.createDialog.description') }}</DialogDescription>
        </DialogHeader>
        <div class="space-y-4">
          <div class="flex gap-2" role="group" :aria-label="t('accounts.qr.method')">
            <Button :variant="createMode === 'qr' ? 'default' : 'outline'" :aria-pressed="createMode === 'qr'" @click="createMode = 'qr'">{{ t('accounts.qr.tab') }}</Button>
            <Button :variant="createMode === 'json' ? 'default' : 'outline'" :aria-pressed="createMode === 'json'" @click="createMode = 'json'">{{ t('accounts.qr.manual') }}</Button>
          </div>
          <div class="grid gap-2">
            <Label for="new-account-name">{{ t('accounts.createDialog.name') }}</Label>
            <Input id="new-account-name" v-model="newName" :disabled="qrActive" maxlength="50" :placeholder="t('accounts.createDialog.namePlaceholder')" />
          </div>
          <div v-if="createMode === 'qr'" class="rounded-xl border bg-slate-50 p-5 text-center space-y-3">
            <div class="mx-auto flex h-56 w-56 items-center justify-center rounded-lg border bg-white p-3">
              <img v-if="qrSession?.qr_image && qrActive" :src="qrSession.qr_image" :alt="t('accounts.qr.imageAlt')" class="h-full w-full object-contain" />
              <LoaderCircle v-else-if="qrActive" class="h-10 w-10 animate-spin text-blue-500" />
              <QrCode v-else class="h-16 w-16 text-slate-300" />
            </div>
            <p role="status" aria-live="polite" class="text-sm font-medium text-slate-700">{{ t(`accounts.qr.${qrSession?.status ?? (isQrStarting ? 'starting' : 'idle')}`) }}</p>
            <p v-if="qrError || qrSession?.error" role="alert" class="text-sm text-red-600">{{ qrError || qrSession?.error }}</p>
            <p class="text-xs text-slate-500">{{ t('accounts.qr.hint') }}</p>
            <Button :disabled="qrActive" @click="handleStartQrLogin">{{ t(qrSession || qrError ? 'accounts.qr.retry' : 'accounts.qr.generate') }}</Button>
          </div>
          <div v-else class="grid gap-2">
            <Label>{{ t('accounts.createDialog.jsonContent') }}</Label>
            <Textarea v-model="newContent" class="min-h-[200px]" :placeholder="t('accounts.createDialog.jsonPlaceholder')" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="isCreateDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button v-if="createMode === 'json'" :disabled="isSaving" @click="handleCreateAccount">
            {{ isSaving ? t('common.saving') : t('common.save') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="isEditDialogOpen">
      <DialogContent class="sm:max-w-[700px]">
        <DialogHeader>
          <DialogTitle>{{ t('accounts.editDialog.title', { name: editName }) }}</DialogTitle>
          <DialogDescription>{{ t('accounts.editDialog.description') }}</DialogDescription>
        </DialogHeader>
        <div class="space-y-4">
          <div class="grid gap-2">
            <Label>{{ t('accounts.createDialog.jsonContent') }}</Label>
            <Textarea v-model="editContent" class="min-h-[200px]" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="isEditDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button :disabled="isSaving" @click="handleUpdateAccount">
            {{ isSaving ? t('common.saving') : t('common.save') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="isDeleteDialogOpen">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{{ t('accounts.deleteDialog.title') }}</DialogTitle>
          <DialogDescription>{{ t('accounts.deleteDialog.description', { name: deleteName }) }}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="isDeleteDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button variant="destructive" :disabled="isSaving" @click="handleDeleteAccount">
            {{ isSaving ? t('accounts.deleteDialog.deleting') : t('accounts.list.delete') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
