const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { createRequire } = require('node:module')
const vm = require('node:vm')
const { test } = require('node:test')

const root = path.resolve(__dirname, '../..')
const frontendRequire = createRequire(path.join(root, 'web-ui/package.json'))

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
