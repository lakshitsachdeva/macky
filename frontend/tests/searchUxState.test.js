import test from 'node:test'
import assert from 'node:assert/strict'

import {
  getQueryCacheVersion,
  invalidateQueryCache,
  queryCacheGet,
  queryCacheSet,
} from '../src/utils/searchCache.js'
import { resolveSearchCase, searchScopeLabel } from '../src/utils/searchScope.js'
import { getSearchIndexNotice } from '../src/utils/searchStatus.js'

test('empty search results are not cached', () => {
  const key = `empty-${Date.now()}`
  assert.equal(queryCacheSet(key, { results: [], debug: null }), false)
  assert.equal(queryCacheGet(key), null)
})

test('cache invalidation rejects stale in-flight writes', () => {
  const key = `stale-${Date.now()}`
  const version = getQueryCacheVersion()
  invalidateQueryCache()

  assert.equal(queryCacheSet(key, { results: [{ id: 'old' }] }, version), false)
  assert.equal(queryCacheGet(key), null)
})

test('invalid stored case selections fall back to all evidence', () => {
  const cases = [{ id: 'case-1', name: 'Incident 2026-04' }]
  assert.deepEqual(resolveSearchCase('deleted-case', cases), { id: '', value: null })
  assert.equal(searchScopeLabel(resolveSearchCase('deleted-case', cases).value), 'all evidence')
  assert.equal(searchScopeLabel(resolveSearchCase('case-1', cases).value), 'case · Incident 2026-04')
})

test('empty stored case selections fall back to all evidence', () => {
  const cases = [{ id: 'empty-case', name: 'Empty incident', evidence_count: 0 }]
  assert.deepEqual(resolveSearchCase('empty-case', cases), { id: '', value: null })
  assert.equal(searchScopeLabel(resolveSearchCase('empty-case', cases).value), 'all evidence')
})

test('search explains when indexing means results are incomplete', () => {
  assert.deepEqual(
    getSearchIndexNotice({
      is_scanning: true,
      scan_progress: { processed: 4, total: 10, remaining: 6 },
    }),
    {
      kind: 'indexing',
      message: 'Indexing files · 4 / 10 — some files may not be searchable yet.',
    },
  )
  assert.deepEqual(
    getSearchIndexNotice({ is_scanning: false, pending_files: 2 }),
    { kind: 'pending', message: 'Search is ready, but 2 files still need indexing.' },
  )
})
