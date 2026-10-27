const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { createRequire } = require('node:module')
const vm = require('node:vm')
const { test } = require('node:test')

const root = path.resolve(__dirname, '../..')
const frontendRequire = createRequire(path.join(root, 'web-ui/package.json'))
const vue = frontendRequire('vue')
const ts = frontendRequire('typescript')

function loadResults(api) {
  const saved = new Map()
  const context = vm.createContext({
    exports: {}, console, setTimeout, clearTimeout,
    localStorage: {
      getItem: (key) => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
      removeItem: (key) => saved.delete(key),
    },
    require(name) {
      if (name === 'vue') return { ...vue, onMounted() {} }
      if (name === 'vue-router') return { useRoute: () => ({ query: {} }) }
      if (name === 'vue-i18n') return { useI18n: () => ({ t: (key) => key }) }
      if (name === '@/api/results') return {
        getResultInsights: async () => null,
        getResultBlacklistRules: async () => ({ keywords: [] }),
        ...api,
      }
      if (name === '@/composables/useWebSocket') return { useWebSocket: () => ({ on() {} }) }
      if (name === '@/api/tasks') return {}
      return frontendRequire(name)
    },
  })
  const source = fs.readFileSync(path.join(root, 'web-ui/src/composables/useResults.ts'), 'utf8')
  vm.runInContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, context)
  const scope = vue.effectScope()
  return { state: scope.run(() => context.exports.useResults()), stop: () => scope.stop() }
}

async function settle() {
  await vue.nextTick()
  await new Promise(setImmediate)
  await vue.nextTick()
}

test('history pagination resets when date or sorting changes and export keeps the date', async () => {
  const calls = []
  let exported
  const { state, stop } = loadResults({
    async getResultContent(file, params) {
      calls.push({ file, ...params })
      return { total_items: 203, items: [] }
    },
    downloadResultExport(file, params) { exported = { file, ...params } },
  })
  try {
    state.selectedFile.value = 'camera_full_data.jsonl'
    await settle()
    assert.equal(state.totalPages.value, 3)
    state.page.value = 2
    await settle()
    assert.equal(calls.at(-1).page, 2)
    state.filters.sort_by = 'price'
    await settle()
    assert.equal(state.page.value, 1)
    assert.equal(calls.at(-1).group_by_day, true)
    state.page.value = 3
    await settle()
    state.crawlDate.value = '2026-10-25'
    await settle()
    assert.equal(calls.at(-1).page, 1)
    assert.equal(calls.at(-1).crawl_date, '2026-10-25')
    state.exportSelectedResults()
    assert.equal(exported.crawl_date, '2026-10-25')
    assert.equal(exported.group_by_day, true)
    assert.equal(exported.page, undefined)
    state.crawlDate.value = ''
    await settle()
    assert.equal(calls.at(-1).crawl_date, undefined)
  } finally { stop() }
})

test('a late response cannot replace results from the current date', async () => {
  const requests = []
  const { state, stop } = loadResults({
    getResultContent(file, params) {
      return new Promise((resolve, reject) => requests.push({ file, params, resolve, reject }))
    },
  })
  try {
    state.selectedFile.value = 'camera_full_data.jsonl'
    await settle()
    state.crawlDate.value = '2026-10-25'
    await settle()
    assert.equal(requests.length, 2)
    requests[1].resolve({ total_items: 1, items: [{ current: true }] })
    await settle()
    requests[0].resolve({ total_items: 200, items: [{ stale: true }] })
    await settle()
    assert.equal(state.totalItems.value, 1)
    assert.equal(state.results.value[0].current, true)
    assert.equal(state.isLoading.value, false)
  } finally { stop() }
})

test('refreshing after records are hidden returns to the last available page', async () => {
  let count = 201
  const calls = []
  const { state, stop } = loadResults({
    getResultFiles: async () => ['camera_full_data.jsonl'],
    async getResultContent(_file, params) {
      calls.push(params)
      return { total_items: count, items: [] }
    },
  })
  try {
    state.selectedFile.value = 'camera_full_data.jsonl'
    await settle()
    state.page.value = 3
    await settle()
    count = 100
    await state.refreshResults()
    await settle()
    assert.equal(state.page.value, 1)
    assert.equal(state.totalPages.value, 1)
    assert.equal(calls.at(-1).page, 1)
  } finally { stop() }
})
