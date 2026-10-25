<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useTasks } from '@/composables/useTasks'
import { useTaskGenerationJob } from '@/composables/useTaskGenerationJob'
import TaskGenerationDialog from '@/components/tasks/TaskGenerationDialog.vue'
import type { Task, TaskUpdate } from '@/types/task.d.ts'
import { parseTaskFormDefaults } from '@/lib/taskFormQuery'
import TaskCreateDialog from '@/components/tasks/TaskCreateDialog.vue'
import TasksTable from '@/components/tasks/TasksTable.vue'
import TaskForm from '@/components/tasks/TaskForm.vue'
import { listAccounts, type AccountItem } from '@/api/accounts'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { toast } from '@/components/ui/toast'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
const { t } = useI18n()

const {
  tasks,
  isLoading,
  error,
  fetchTasks,
  removeTask,
  updateTask,
  startTask,
  stopTask,
  stoppingTaskIds,
} = useTasks()
const route = useRoute()

// State for dialogs
const isEditDialogOpen = ref(false)
const isCriteriaDialogOpen = ref(false)
const isEditSubmitting = ref(false)
const isUpdateProgressOpen = ref(false)
const { activeJob: updateJob, pollingError, beginPolling, clearJob } = useTaskGenerationJob()
const selectedTask = ref<Task | null>(null)
const criteriaTask = ref<Task | null>(null)
const criteriaDescription = ref('')
const isCriteriaSubmitting = ref(false)
const isDeleteDialogOpen = ref(false)
const taskToDeleteId = ref<number | null>(null)
const accountOptions = ref<AccountItem[]>([])

const taskToDelete = computed(() => {
  if (taskToDeleteId.value === null) return null
  return tasks.value.find((task) => task.id === taskToDeleteId.value) || null
})
const editDefaults = computed(() => parseTaskFormDefaults(route.query))

function handleDeleteTask(taskId: number) {
  taskToDeleteId.value = taskId
  isDeleteDialogOpen.value = true
}

async function handleConfirmDeleteTask() {
  if (!taskToDelete.value) {
    toast({ title: t('tasks.toasts.notFound'), variant: 'destructive' })
    isDeleteDialogOpen.value = false
    return
  }
  try {
    await removeTask(taskToDelete.value.id)
    toast({ title: t('tasks.toasts.deleted') })
  } catch (e) {
    toast({
      title: t('tasks.toasts.deleteFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  } finally {
    isDeleteDialogOpen.value = false
    taskToDeleteId.value = null
  }
}

function handleEditTask(task: Task) {
  selectedTask.value = task
  isEditDialogOpen.value = true
}

watch(
  () => [route.query.edit, tasks.value],
  () => {
    const editTaskId = typeof route.query.edit === 'string' ? Number(route.query.edit) : NaN
    if (!Number.isFinite(editTaskId)) return
    const match = tasks.value.find((task) => task.id === editTaskId)
    if (!match) return
    selectedTask.value = match
    isEditDialogOpen.value = true
  },
  { immediate: true }
)

async function handleUpdateTask(data: TaskUpdate) {
  if (!selectedTask.value) return
  isEditSubmitting.value = true
  try {
    const result = await updateTask(selectedTask.value.id, data)
    isEditDialogOpen.value = false
    if (result?.job) {
      clearJob()
      isUpdateProgressOpen.value = true
      beginPolling(result.job)
    } else {
      toast({ title: t('tasks.toasts.updated') })
    }
  }
  catch (e) {
    toast({
      title: t('tasks.toasts.updateFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  }
  finally {
    isEditSubmitting.value = false
  }
}

function handleOpenCriteriaDialog(task: Task) {
  criteriaTask.value = task
  criteriaDescription.value = task.description || ''
  isCriteriaDialogOpen.value = true
}

async function handleRefreshCriteria() {
  if (!criteriaTask.value) return
  if (!criteriaDescription.value.trim()) {
    toast({
      title: t('tasks.toasts.descriptionRequired'),
      description: t('tasks.criteria.descriptionRequired'),
      variant: 'destructive',
    })
    return
  }

  isCriteriaSubmitting.value = true
  try {
    const result = await updateTask(criteriaTask.value.id, { description: criteriaDescription.value, regenerate_criteria: true })
    isCriteriaDialogOpen.value = false
    if (result?.job) {
      clearJob()
      isUpdateProgressOpen.value = true
      beginPolling(result.job)
    }
  } catch (e) {
    toast({
      title: t('tasks.toasts.regenerateFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  } finally {
    isCriteriaSubmitting.value = false
  }
}

async function handleStartTask(taskId: number) {
  try {
    await startTask(taskId)
  } catch (e) {
    toast({
      title: t('tasks.toasts.startFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  }
}

async function handleStopTask(taskId: number) {
  try {
    await stopTask(taskId)
  } catch (e) {
    toast({
      title: t('tasks.toasts.stopFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  }
}

async function handleToggleEnabled(task: Task, enabled: boolean) {
  const previous = task.enabled
  task.enabled = enabled
  try {
    await updateTask(task.id, { enabled })
  } catch (e) {
    task.enabled = previous
    toast({
      title: t('tasks.toasts.toggleFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  }
}

async function fetchAccountOptions() {
  try {
    accountOptions.value = await listAccounts()
  } catch (e) {
    toast({
      title: t('tasks.toasts.loadAccountsFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  }
}

onMounted(fetchAccountOptions)

watch(() => updateJob.value?.status, async (status, previous) => {
  if (!status || status === previous) return
  if (status === 'completed') {
    const completed = updateJob.value
    await fetchTasks({ silent: true })
    toast({ title: t('tasks.toasts.updated'), description: completed?.message })
    if (updateJob.value?.job_id === completed?.job_id) {
      isUpdateProgressOpen.value = false
      clearJob()
    }
  } else if (status === 'failed') {
    toast({ title: t('tasks.toasts.updateFailed'), description: updateJob.value?.error || undefined, variant: 'destructive' })
  }
})
watch(pollingError, (error) => {
  if (error) toast({ title: t('tasks.toasts.progressFailed'), description: error.message, variant: 'destructive' })
})
</script>

<template>
  <div>
    <TaskGenerationDialog v-model:open="isUpdateProgressOpen" :job="updateJob" />
    <div class="flex justify-between items-center mb-6">
      <h1 class="text-2xl font-bold text-gray-800">
        {{ t('tasks.title') }}
      </h1>
      <TaskCreateDialog :account-options="accountOptions" @created="fetchTasks" />
    </div>

    <!-- Edit Task Dialog -->
    <Dialog v-model:open="isEditDialogOpen">
      <DialogContent class="sm:max-w-[640px] max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{{ t('tasks.editDialog.title', { task: selectedTask?.task_name || "" }) }}</DialogTitle>
        </DialogHeader>
        <TaskForm
          v-if="selectedTask"
          mode="edit"
          :initial-data="selectedTask"
          :account-options="accountOptions"
          :default-values="editDefaults"
          @submit="(data) => handleUpdateTask(data as TaskUpdate)"
        />
        <DialogFooter>
          <Button type="submit" form="task-form" :disabled="isEditSubmitting">
            {{ isEditSubmitting ? t('common.saving') : t('tasks.editDialog.save') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- Refresh Criteria Dialog -->
    <Dialog v-model:open="isCriteriaDialogOpen">
      <DialogContent class="sm:max-w-[600px]">
        <DialogHeader>
          <DialogTitle>{{ t('tasks.criteria.title') }}</DialogTitle>
          <DialogDescription>
            {{ t('tasks.criteria.description') }}
          </DialogDescription>
        </DialogHeader>
        <div class="grid gap-3">
          <label class="text-sm font-medium text-gray-700">{{ t('tasks.form.description') }}</label>
          <Textarea
            v-model="criteriaDescription"
            class="min-h-[140px]"
            :placeholder="t('tasks.form.descriptionPlaceholder')"
          />
        </div>
        <DialogFooter>
          <Button variant="outline" @click="isCriteriaDialogOpen = false">
            {{ t('common.cancel') }}
          </Button>
          <Button :disabled="isCriteriaSubmitting" @click="handleRefreshCriteria">
            {{ isCriteriaSubmitting ? t('tasks.criteria.generating') : t('tasks.criteria.action') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <div v-if="error" class="app-alert-error mb-4" role="alert">
      <strong class="font-bold">{{ t('common.error') }}</strong>
      <span class="block sm:inline">{{ error.message }}</span>
    </div>

    <TasksTable
      :tasks="tasks"
      :is-loading="isLoading"
      :stopping-ids="stoppingTaskIds"
      @delete-task="handleDeleteTask"
      @edit-task="handleEditTask"
      @run-task="handleStartTask"
      @stop-task="handleStopTask"
      @refresh-criteria="handleOpenCriteriaDialog"
      @toggle-enabled="handleToggleEnabled"
    />

    <Dialog v-model:open="isDeleteDialogOpen">
      <DialogContent class="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>{{ t('tasks.deleteDialog.title') }}</DialogTitle>
          <DialogDescription>
            {{ taskToDelete ? t('tasks.deleteDialog.descriptionWithTask', { task: taskToDelete.task_name }) : t('tasks.deleteDialog.descriptionFallback') }}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="isDeleteDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button variant="destructive" @click="handleConfirmDeleteTask">{{ t('tasks.deleteDialog.confirm') }}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
