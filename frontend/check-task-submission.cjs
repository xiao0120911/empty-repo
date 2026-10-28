// Offline regression: execute the actual App.vue setup functions with controlled refs and API promises.
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, 'src/App.vue'), 'utf8')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm, '')
const js = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const makeSetup = new Function('ref', 'computed', 'watch', 'defineComponent', 'h', 'onMounted', 'onUnmounted', 'api', 'setCsrf', 'requestId', js + `
return { user, busy, error, prompt, runs, active, conversation, selectedConversationId, conversationLoading,
  submitTask, submitOnce, refreshActivity, selectRun, openConversation, newConversation, clearIdentity, resultProvenance,
  retryCount:()=>submissionRetry.size, versions:()=>({identityVersion,selectionVersion}),
  waitForRefresh:task=>{refreshing=task} }
`)
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = () => new Promise(resolve => setImmediate(resolve))
const run = (id, prompt = 'Dining-area cameras', cid = 'context-a', owner = 'owner-a') => ({
  id, prompt, conversation_id: cid, owner_id: owner, mode: 'test', status: 'completed',
  created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z', events: [],
  result: { report: `Report for ${id}`, fresh_query_required: true, current_evidence_count: 1 }
})
const detail = (cid, items = [], owner = 'owner-a') => ({
  id: cid, owner_id: owner, title: cid, created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z', runs: items
})
function harness() {
  const ref = initial => {
    let value = initial
    const listeners = []
    return { listeners, get value() { return value }, set value(next) {
      if (Object.is(value, next)) return
      const old = value; value = next
      for (const listener of listeners) listener(next, old)
    } }
  }
  const computed = getter => ({ get value() { return getter() } })
  const watch = (target, callback) => { target.listeners.push(callback) }
  const calls = [], records = new Map(), contexts = new Map()
  let sequence = 0, handler = () => undefined
  const api = (url, method = 'GET', body) => {
    calls.push({ url, method, body })
    let response
    try { response = handler(url, method, body) } catch (error) { return Promise.reject(error) }
    if (response !== undefined) return Promise.resolve(response)
    if (method === 'POST') {
      const accepted = run(`accepted-${++sequence}`, body.prompt, body.conversation_id || null)
      records.set(accepted.id, accepted)
      if (accepted.conversation_id) contexts.set(accepted.conversation_id, detail(accepted.conversation_id, [accepted]))
      return Promise.resolve(accepted)
    }
    if (url === '/runs') return Promise.resolve([...records.values()])
    if (url === '/conversations') return Promise.resolve([...contexts.values()])
    if (url.startsWith('/runs/')) return Promise.resolve(records.get(decodeURIComponent(url.slice(6))))
    if (url.startsWith('/conversations/')) return Promise.resolve(contexts.get(decodeURIComponent(url.slice(15))))
    throw new Error(`Unexpected API call: ${method} ${url}`)
  }
  const state = makeSetup(ref, computed, watch, value => value, () => ({}), () => {}, () => {}, api, () => {}, () => `key-${++sequence}`)
  state.user.value = { id: 'owner-a', role: 'admin', nickname: 'Test' }
  const old = run('old', 'All cameras')
  records.set(old.id, old); contexts.set('context-a', detail('context-a', [old]))
  state.selectedConversationId.value = 'context-a'; state.conversation.value = contexts.get('context-a')
  state.runs.value = [old]; state.active.value = old; state.prompt.value = 'Dining-area cameras'
  return { ...state, calls, records, contexts, intercept: fn => { handler = fn } }
}
const tests = []
const test = (name, fn) => tests.push([name, fn])
const posts = state => state.calls.filter(call => call.method === 'POST')

test('click snapshot survives a refresh wait and preserves newly edited text', async () => {
  const s = harness(), wait = deferred()
  s.waitForRefresh(wait.promise)
  const selection = s.versions().selectionVersion
  const task = s.submitTask()
  assert.equal(posts(s).length, 0)
  assert.equal(s.active.value, null)
  assert.equal(s.versions().selectionVersion, selection + 1)
  await tick()
  assert.equal(s.active.value, null)
  s.prompt.value = 'Newly typed query'
  wait.resolve(); await task
  assert.equal(posts(s)[0].body.prompt, 'Dining-area cameras')
  assert.equal(posts(s)[0].body.conversation_id, 'context-a')
  assert.equal(s.prompt.value, 'Newly typed query')
  assert.equal(s.active.value.prompt, 'Dining-area cameras')
})

test('selection change while waiting for refresh never posts the stale task', async () => {
  const s = harness(), wait = deferred()
  s.waitForRefresh(wait.promise)
  const task = s.submitTask()
  s.newConversation(); s.prompt.value = 'Independent query'
  wait.resolve(); await task
  assert.equal(posts(s).length, 0)
  assert.equal(s.prompt.value, 'Independent query')
  assert.equal(s.active.value, null)
})

test('identity change while waiting for refresh never posts or clears new identity state', async () => {
  const s = harness(), wait = deferred()
  s.waitForRefresh(wait.promise)
  const task = s.submitTask()
  s.clearIdentity(); s.user.value = { id: 'owner-b' }; s.prompt.value = 'Owner B query'; s.busy.value = true
  wait.resolve(); await task
  assert.equal(posts(s).length, 0)
  assert.equal(s.prompt.value, 'Owner B query')
  assert.equal(s.busy.value, true)
})

test('selection change during context preflight prevents posting and stale errors', async () => {
  const s = harness(), wait = deferred()
  s.intercept(url => url === '/conversations/context-a' ? wait.promise : undefined)
  const task = s.submitTask()
  assert.equal(s.active.value, null)
  s.newConversation(); s.prompt.value = 'Unrelated query'
  wait.reject(new Error('Old preflight failed')); await task
  assert.equal(posts(s).length, 0)
  assert.equal(s.prompt.value, 'Unrelated query')
  assert.equal(s.error.value, '')
})

test('identity change during context preflight prevents posting', async () => {
  const s = harness(), wait = deferred()
  s.intercept(url => url === '/conversations/context-a' ? wait.promise : undefined)
  const task = s.submitTask()
  s.clearIdentity(); s.user.value = { id: 'owner-b' }; s.prompt.value = 'Owner B query'
  wait.resolve(detail('context-a')); await task
  assert.equal(posts(s).length, 0)
  assert.equal(s.prompt.value, 'Owner B query')
  assert.equal(s.conversation.value, null)
})

test('submission failure clears the old right result and retains the exact retry key', async () => {
  const s = harness(), wait = deferred()
  let attempts = 0
  s.intercept((url, method) => method === 'POST' && ++attempts === 1 ? wait.promise : undefined)
  const task = s.submitTask(); await tick()
  assert.equal(s.active.value, null)
  const first = posts(s)[0].body.idempotency_key
  wait.reject(new Error('Submission unavailable')); await task
  assert.equal(s.active.value, null)
  assert.equal(s.prompt.value, 'Dining-area cameras')
  assert.match(s.error.value, /Submission unavailable/)
  assert.equal(s.retryCount(), 1)
  await s.submitTask()
  assert.equal(posts(s)[1].body.idempotency_key, first)
  assert.equal(s.retryCount(), 0)
  assert.equal(s.prompt.value, '')
})

test('prompt mismatch is never displayed as current and retries keep the original key', async () => {
  const s = harness()
  let rejectResponse = true
  s.intercept((url, method) => method === 'POST' && rejectResponse ? s.records.get('old') : undefined)
  await s.submitTask()
  const key = posts(s)[0].body.idempotency_key
  assert.equal(s.active.value, null)
  assert.equal(s.prompt.value, 'Dining-area cameras')
  assert.equal(s.error.value, '服务端返回的任务与提交内容不一致，未加载旧结果。直接重试将沿用同一请求标识。')
  assert.equal(s.retryCount(), 1)
  rejectResponse = false; await s.submitTask()
  assert.equal(posts(s)[1].body.idempotency_key, key)
  assert.equal(s.retryCount(), 0)
})

test('wrong accepted owner or context does not display a result or drop retry keys', async () => {
  for (const accepted of [run('wrong-owner', 'Dining-area cameras', 'context-a', 'owner-b'), run('wrong-context', 'Dining-area cameras', 'context-b')]) {
    const s = harness()
    s.intercept((url, method) => method === 'POST' ? accepted : undefined)
    await s.submitTask()
    assert.equal(s.active.value, null)
    assert.equal(s.prompt.value, 'Dining-area cameras')
    assert.equal(s.error.value, '服务端返回的任务与提交时的身份或上下文不一致，未加载该结果。')
    assert.equal(s.retryCount(), 1)
  }
})

test('accepted run stays focused even when its conversation has a later run', async () => {
  const s = harness(), accepted = run('exact-accepted'), later = run('later', 'Different query')
  let reads = 0
  s.records.set(accepted.id, accepted); s.records.set(later.id, later)
  s.intercept((url, method) => {
    if (method === 'POST') return accepted
    if (url === '/conversations/context-a' && ++reads > 1) return detail('context-a', [accepted, later])
  })
  await s.submitTask()
  assert.equal(s.active.value.id, 'exact-accepted')
  assert(!s.calls.some(call => call.url === '/runs/later'))
})

test('old poll cannot replace accepted task, its list, or selected context', async () => {
  const s = harness(), wait = deferred()
  let polling = false
  s.intercept((url, method) => polling && method === 'GET' ? wait.promise.then(() => {
    if (url === '/runs') return [run('old', 'All cameras')]
    if (url === '/conversations') return [detail('context-a')]
    if (url === '/conversations/context-a') return detail('context-a', [run('old', 'All cameras')])
    if (url === '/runs/old') return run('old', 'All cameras')
    throw new Error(`Unexpected poll: ${url}`)
  }) : undefined)
  const task = s.submitTask()
  // Start a delayed poll after submission began, before POST acceptance.
  s.active.value = run('old', 'All cameras'); polling = true
  const poll = s.refreshActivity(); polling = false
  await tick()
  assert.equal(s.active.value.id.startsWith('accepted-'), true)
  const acceptedId = s.active.value.id
  wait.resolve(); await Promise.all([poll, task])
  assert.equal(s.active.value.id, acceptedId)
  assert(s.runs.value.some(item => item.id === acceptedId))
  assert(s.conversation.value.runs.some(item => item.id === acceptedId))
})

test('old pre-click poll is awaited and cannot restore old output after submission', async () => {
  const s = harness(), wait = deferred()
  let first = true
  s.intercept(url => url === '/runs/old' && first ? (first = false, wait.promise) : undefined)
  const poll = s.refreshActivity(), task = s.submitTask()
  assert.equal(posts(s).length, 0)
  assert.equal(s.active.value, null)
  await tick()
  assert.equal(s.active.value, null)
  wait.resolve(run('old', 'All cameras')); await Promise.all([poll, task])
  assert.equal(s.active.value.prompt, 'Dining-area cameras')
})

test('changed criteria get a different key without discarding an unaccepted request', async () => {
  const s = harness()
  s.intercept((url, method) => method === 'POST' ? Promise.reject(new Error('Offline')) : undefined)
  await s.submitTask()
  const first = posts(s)[0].body.idempotency_key
  s.prompt.value = 'Cooking-area cameras'; await s.submitTask()
  assert.notEqual(posts(s)[1].body.idempotency_key, first)
  s.prompt.value = 'Dining-area cameras'; await s.submitTask()
  assert.equal(posts(s)[2].body.idempotency_key, first)
  assert.equal(s.retryCount(), 2)
  s.newConversation()
  assert.equal(s.retryCount(), 0)
  s.clearIdentity()
  assert.equal(s.retryCount(), 0)
})

test('standalone transport retry retains its key, but explicit new task gets a different key for identical text', async () => {
  const s = harness(), text = '查询就餐区的摄像头信息'
  s.newConversation(); s.prompt.value = text
  s.intercept((url, method) => method === 'POST' ? Promise.reject(new Error('传输失败')) : undefined)
  await s.submitTask()
  const first = posts(s)[0].body.idempotency_key
  assert.equal(s.active.value, null)
  assert.equal(s.prompt.value, text)
  assert.equal(s.retryCount(), 1)
  assert.equal(posts(s)[0].body.conversation_id, undefined)
  await s.submitTask()
  assert.equal(posts(s)[1].body.idempotency_key, first)
  assert.equal(s.retryCount(), 1)
  s.newConversation()
  assert.equal(s.retryCount(), 0)
  s.prompt.value = text
  s.intercept(() => undefined)
  await s.submitTask()
  assert.equal(posts(s)[2].body.prompt, text)
  assert.equal(posts(s)[2].body.conversation_id, undefined)
  assert.notEqual(posts(s)[2].body.idempotency_key, first)
  assert.equal(s.active.value.prompt, text)
  assert.equal(s.retryCount(), 0)
})

test('explicit new-task reset protects a new retry entry from an old same-text acceptance', async () => {
  const s = harness(), old = deferred(), next = deferred(), body = { prompt: '查询就餐区的摄像头信息' }
  s.newConversation()
  let attempts = 0
  s.intercept((url, method) => method === 'POST' ? (++attempts === 1 ? old.promise : next.promise) : undefined)
  const first = s.submitOnce('/runs', body)
  const oldKey = posts(s)[0].body.idempotency_key
  s.newConversation()
  const second = s.submitOnce('/runs', body)
  assert.notEqual(posts(s)[1].body.idempotency_key, oldKey)
  old.resolve(run('old-accepted', body.prompt, null)); await first
  assert.equal(s.retryCount(), 1)
  next.resolve(run('new-accepted', body.prompt, null)); await second
  assert.equal(s.retryCount(), 0)
})

test('Chinese context errors stop submission and preserve the submitted text', async () => {
  const mismatch = harness()
  mismatch.intercept(url => url === '/conversations/context-a' ? detail('context-b') : undefined)
  await mismatch.submitTask()
  assert.equal(mismatch.error.value, '返回的任务上下文与所选任务不一致，已停止提交。')
  assert.equal(posts(mismatch).length, 0)
  assert.equal(mismatch.active.value, null)
  assert.equal(mismatch.prompt.value, 'Dining-area cameras')
  const unfinished = harness()
  unfinished.intercept(url => url === '/conversations/context-a' ? detail('context-a', [{ ...run('running'), status: 'running' }]) : undefined)
  await unfinished.submitTask()
  assert.equal(unfinished.error.value, '当前上下文仍有未结束任务，请先完成执行、人工确认或结果核对后再提交。')
  assert.equal(posts(unfinished).length, 0)
  assert.equal(unfinished.active.value, null)
  const loading = harness()
  loading.intercept(url => url === '/conversations/context-a' ? detail('context-b') : undefined)
  await assert.rejects(loading.openConversation('context-a'), { message: '返回的任务上下文与所选任务不一致。' })
})

test('text edited and restored during POST is not cleared on acceptance', async () => {
  const s = harness(), wait = deferred(), accepted = run('accepted')
  s.records.set(accepted.id, accepted)
  s.intercept((url, method) => method === 'POST' ? wait.promise : undefined)
  const task = s.submitTask(); await tick()
  s.prompt.value = 'Edited'; s.prompt.value = 'Dining-area cameras'
  wait.resolve(accepted); await task
  assert.equal(s.active.value.id, 'accepted')
  assert.equal(s.prompt.value, 'Dining-area cameras')
})

test('stale POST selection response cannot replace unrelated result or text', async () => {
  const s = harness(), wait = deferred()
  s.intercept((url, method) => method === 'POST' ? wait.promise : undefined)
  const task = s.submitTask(); await tick()
  s.newConversation(); s.prompt.value = 'Unrelated text'; s.active.value = run('unrelated')
  wait.resolve(run('accepted')); await task
  assert.equal(s.active.value.id, 'unrelated')
  assert.equal(s.prompt.value, 'Unrelated text')
  assert.equal(s.error.value, '')
  assert.equal(s.retryCount(), 0)
})

test('stale identity POST response cannot erase a new identity retry entry or UI', async () => {
  const s = harness(), old = deferred(), next = deferred(), body = { prompt: 'Same prompt' }
  let calls = 0
  s.intercept((url, method) => method === 'POST' ? (++calls === 1 ? old.promise : next.promise) : undefined)
  const first = s.submitOnce('/runs', body)
  s.clearIdentity(); s.user.value = { id: 'owner-b' }; s.prompt.value = 'Owner B text'
  const second = s.submitOnce('/runs', body)
  old.resolve(run('owner-a-run', 'Same prompt', null)); await first
  assert.equal(s.retryCount(), 1)
  assert.equal(s.prompt.value, 'Owner B text')
  next.resolve(run('owner-b-run', 'Same prompt', null, 'owner-b')); await second
  assert.equal(s.retryCount(), 0)
})

test('stale POST rejection cannot write an error into a new selection', async () => {
  const s = harness(), wait = deferred()
  s.intercept((url, method) => method === 'POST' ? wait.promise : undefined)
  const task = s.submitTask(); await tick()
  s.newConversation(); s.prompt.value = 'Unrelated text'
  wait.reject(new Error('Old submission error')); await task
  assert.equal(s.error.value, '')
  assert.equal(s.prompt.value, 'Unrelated text')
  assert.equal(s.retryCount(), 0)
})

test('selection during accepted-context loading stops trailing refresh and preserves text', async () => {
  const s = harness(), wait = deferred()
  let reads = 0
  s.intercept(url => url === '/conversations/context-a' && ++reads === 2 ? wait.promise : undefined)
  const task = s.submitTask(); await tick()
  s.newConversation(); s.prompt.value = 'Unrelated text'
  const count = s.calls.length
  wait.resolve(detail('context-a', [run('accepted')])); await task
  assert.equal(s.calls.length, count)
  assert.equal(s.active.value, null)
  assert.equal(s.prompt.value, 'Unrelated text')
  assert.equal(s.conversationLoading.value, false)
})

test('history selection respects text edits and selection or identity changes during load', async () => {
  for (const change of ['edit', 'selection', 'identity']) {
    const s = harness(), wait = deferred(), historical = run('historical', 'Historical query', null)
    s.intercept(url => url === '/runs/historical' ? wait.promise : undefined)
    const task = s.selectRun(historical)
    if (change === 'selection') s.newConversation()
    if (change === 'identity') { s.clearIdentity(); s.user.value = { id: 'owner-b' } }
    s.prompt.value = 'Keep this text'
    wait.resolve(historical); await task
    assert.equal(s.prompt.value, 'Keep this text')
    if (change !== 'edit') assert.equal(s.active.value, null)
    else assert.equal(s.active.value.id, historical.id)
  }
})

test('refresh holds the lock until all requests settle and obeys identity guards', async () => {
  const s = harness(), wait = deferred()
  s.intercept(url => url === '/runs' ? Promise.reject(new Error('Poll failed')) : url === '/runs/old' ? wait.promise : undefined)
  const poll = s.refreshActivity()
  await tick()
  const count = s.calls.length
  const duplicate = s.refreshActivity()
  assert.equal(s.calls.length, count)
  wait.resolve(run('old')); await assert.rejects(poll, /Poll failed/); await assert.rejects(duplicate, /Poll failed/)
  const next = deferred()
  s.intercept(url => url === '/runs/old' ? next.promise : undefined)
  const stale = s.refreshActivity()
  s.clearIdentity(); s.user.value = { id: 'owner-b' }; s.runs.value = [run('owner-b', 'B', null, 'owner-b')]
  next.resolve(run('old')); await stale
  assert.equal(s.runs.value[0].id, 'owner-b')
})

test('accepted context loading errors remain visible, but stale poll errors are ignored', async () => {
  const s = harness()
  let reads = 0
  s.intercept(url => url === '/conversations/context-a' && ++reads === 2 ? Promise.reject(new Error('Accepted context unavailable')) : undefined)
  await s.submitTask()
  assert.match(s.error.value, /Accepted context unavailable/)
  assert.equal(s.active.value.prompt, 'Dining-area cameras')
  assert.equal(s.retryCount(), 0)
  const stale = harness(), wait = deferred()
  stale.intercept(url => url === '/runs/old' ? wait.promise : undefined)
  const poll = stale.refreshActivity()
  stale.newConversation(); stale.prompt.value = 'Unrelated text'
  wait.reject(new Error('Old poll error')); await poll
  assert.equal(stale.error.value, '')
  assert.equal(stale.prompt.value, 'Unrelated text')
})

test('busy submission is ignored and provenance never promotes old or absent evidence', async () => {
  const s = harness(); s.busy.value = true; await s.submitTask()
  assert.equal(s.calls.length, 0)
  assert.equal(s.resultProvenance({ result: { context_reused: true, source_run_id: 'source-123' } }), '仅对历史结果进行排版，未发起新查询。 来源任务：source-123。')
  assert.equal(s.resultProvenance({ result: { context_reused: true } }), '仅对历史结果进行排版，未发起新查询。')
  assert.equal(s.resultProvenance({ result: { fresh_query_required: true, current_evidence_count: 2 } }), '本次执行已有 2 条证据，但不代表查询完整或成功。')
  assert.equal(s.resultProvenance({ status: 'failed', result: { fresh_query_required: true, current_evidence_count: 0, evidence_count: 100 } }), '新查询执行失败，且没有本次执行证据；数据未刷新。')
  assert.equal(s.resultProvenance({ result: { fresh_query_required: true, evidence_count: 100 } }), '已请求新查询，但暂无本次执行证据，无法确认数据已刷新。')
  assert.equal(s.resultProvenance({ result: {} }), '未提供查询来源信息，无法确认数据已刷新。')
  assert(source.includes('上下文仅辅助理解引用；新增查询条件会重新查询，仅纯排版请求复用历史结果。'))
  assert(source.includes('新任务：不沿用历史上下文或结果。'))
  assert(source.includes('resultProvenance(active)'))
})

async function main() {
  for (const [name, fn] of tests) {
    await fn()
    console.log(`PASS ${name}`)
  }
  console.log(`${tests.length} offline task submission/refresh regressions passed; no browser or backend used.`)
}
main().catch(error => { console.error(error); process.exitCode = 1 })
