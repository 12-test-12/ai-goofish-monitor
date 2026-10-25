import { computed, onScopeDispose, ref } from 'vue'
import { getTaskGenerationJob } from '@/api/tasks'
import type { TaskGenerationJob } from '@/types/task.d.ts'

const POLL_INTERVAL_MS = 800

function isTerminalStatus(status: TaskGenerationJob['status']) {
  return status === 'completed' || status === 'failed'
}

export function useTaskGenerationJob() {
  const activeJob = ref<TaskGenerationJob | null>(null)
  const pollingError = ref<Error | null>(null)
  const isPolling = ref(false)
  let pollTimer: ReturnType<typeof window.setTimeout> | null = null
  let pollSeq = 0

  function clearTimer() {
    if (pollTimer === null) return
    window.clearTimeout(pollTimer)
    pollTimer = null
  }

  async function refreshJob() {
    if (!activeJob.value || !isPolling.value) return
    const currentSeq = ++pollSeq
    const targetJobId = activeJob.value.job_id
    try {
      const nextJob = await getTaskGenerationJob(targetJobId)
      if (pollSeq !== currentSeq || !isPolling.value || activeJob.value?.job_id !== targetJobId) {
        return
      }
      activeJob.value = nextJob
      pollingError.value = null
      if (isTerminalStatus(nextJob.status)) {
        isPolling.value = false
        clearTimer()
        return
      }
      scheduleNextPoll()
    } catch (error) {
      if (pollSeq !== currentSeq || !isPolling.value || activeJob.value?.job_id !== targetJobId) {
        return
      }
      isPolling.value = false
      clearTimer()
      pollingError.value = error as Error
    }
  }

  function scheduleNextPoll() {
    clearTimer()
    pollTimer = window.setTimeout(() => {
      void refreshJob()
    }, POLL_INTERVAL_MS)
  }

  function beginPolling(job: TaskGenerationJob) {
    pollSeq++
    activeJob.value = job
    pollingError.value = null
    if (isTerminalStatus(job.status)) {
      isPolling.value = false
      clearTimer()
      return
    }
    isPolling.value = true
    scheduleNextPoll()
  }

  function clearJob() {
    pollSeq++
    activeJob.value = null
    pollingError.value = null
    isPolling.value = false
    clearTimer()
  }

  onScopeDispose(clearJob)

  return {
    activeJob,
    pollingError,
    isGenerating: computed(() => isPolling.value),
    beginPolling,
    clearJob,
    refreshJob,
  }
}
