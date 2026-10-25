const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { createRequire } = require('node:module')
const vm = require('node:vm')
const { test } = require('node:test')

const root = path.resolve(__dirname, '../..')
const frontendRequire = createRequire(path.join(root, 'web-ui/package.json'))

test('editing tasks handles queued AI updates without treating the job as a saved task', async () => {
  const ts = frontendRequire('typescript')
  const vue = frontendRequire('vue')
  const existing = { id: 7, task_name: 'camera', description: 'old' }
  let result = { message: 'queued', job: { job_id: 'update-job', status: 'queued', operation: 'update' } }
  const api = { updateTask: async () => result }
  const context = vm.createContext({
    exports: {}, console,
    require(name) {
      if (name === 'vue') return { ...vue, onMounted() {} }
      if (name === '@/api/tasks') return api
      if (name === '@/composables/useWebSocket') return { useWebSocket: () => ({ on() {} }) }
      return frontendRequire(name)
    },
  })
  const source = fs.readFileSync(path.join(root, 'web-ui/src/composables/useTasks.ts'), 'utf8')
  vm.runInContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, context)
  const tasks = context.exports.useTasks()
  tasks.tasks.value = [existing]
  assert.equal((await tasks.updateTask(7, { description: 'new' })).job.job_id, 'update-job')
  assert.equal(tasks.tasks.value.length, 1)
  assert.equal(tasks.tasks.value[0].description, 'old')
  result = { message: 'saved', task: { ...existing, description: 'new' } }
  await tasks.updateTask(7, { description: 'new' })
  assert.equal(tasks.tasks.value[0].description, 'new')
})

test('extension captures only HTTP(S) tabs on Goofish hosts', async () => {
  let activeUrl
  const context = vm.createContext({
    URL,
    chrome: {
      tabs: { query: async () => [{ id: 1, url: activeUrl }] },
      runtime: { onMessage: { addListener() {} } },
    },
  })
  vm.runInContext(fs.readFileSync(path.join(root, 'chrome-extension/background.js'), 'utf8'), context)
  for (const url of ['https://goofish.com/', 'https://www.goofish.com/', 'https://h5.goofish.com/']) {
    activeUrl = url
    assert.equal((await context.getActiveGoofishTab()).url, url)
  }
  for (const url of [
    'https://evil.example/?next=goofish.com',
    'https://goofish.com.evil.example/',
    'https://fakegoofish.com/',
    'https://goofish.com@evil.example/',
    'ftp://goofish.com/',
  ]) {
    activeUrl = url
    await assert.rejects(context.getActiveGoofishTab())
  }
})

test('toast state reacts to add, update, dismiss and removal without unwrapping VNodes', () => {
  const ts = frontendRequire('typescript')
  const { h } = frontendRequire('vue')
  const timers = []
  const context = vm.createContext({
    exports: {},
    require: frontendRequire,
    setTimeout(callback) { timers.push(callback); return timers.length },
  })
  const source = fs.readFileSync(path.join(root, 'web-ui/src/components/ui/toast/use-toast.ts'), 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  vm.runInContext(compiled.outputText, context)
  const { toasts, toast } = context.exports.useToast()
  assert.equal(toasts.value.length, 0)
  const description = h('strong', 'message')
  const handle = toast({ title: 'first', description })
  assert.equal(toasts.value[0].title, 'first')
  assert.equal(toasts.value[0].description, description)
  handle.update({ id: handle.id, title: 'updated', description })
  assert.equal(toasts.value[0].title, 'updated')
  handle.dismiss()
  assert.equal(toasts.value[0].open, false)
  timers[0]()
  assert.equal(toasts.value.length, 0)
  toast({ title: 'old' })
  toast({ title: 'new' })
  assert.equal(toasts.value.length, 1)
  assert.equal(toasts.value[0].title, 'new')
})

test('useTaskGenerationJob discards stale in-flight responses and prevents race conditions', async () => {
  const ts = frontendRequire('typescript')
  const vue = frontendRequire('vue')
  let resolveJob1
  const job1Promise = new Promise((resolve) => { resolveJob1 = resolve })
  const api = {
    getTaskGenerationJob: async (jobId) => {
      if (jobId === 'job-1') return await job1Promise
      return { job_id: 'job-2', status: 'completed' }
    },
  }
  const context = vm.createContext({
    exports: {},
    console,
    window: {
      setTimeout: (fn) => setTimeout(fn, 0),
      clearTimeout: (id) => clearTimeout(id),
    },
    require(name) {
      if (name === 'vue') return { ...vue, onScopeDispose() {} }
      if (name === '@/api/tasks') return api
      return frontendRequire(name)
    },
  })
  const source = fs.readFileSync(path.join(root, 'web-ui/src/composables/useTaskGenerationJob.ts'), 'utf8')
  vm.runInContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, context)
  const { activeJob, beginPolling, clearJob, refreshJob } = context.exports.useTaskGenerationJob()
  beginPolling({ job_id: 'job-1', status: 'running' })
  // In-flight refresh for job-1
  const refreshPromise = refreshJob()
  // Before job-1 returns, user cleared job
  clearJob()
  assert.equal(activeJob.value, null)
  // job-1 finishes late
  resolveJob1({ job_id: 'job-1', status: 'completed' })
  await refreshPromise
  // activeJob must still be null, not overwritten by stale job-1
  assert.equal(activeJob.value, null)
})
