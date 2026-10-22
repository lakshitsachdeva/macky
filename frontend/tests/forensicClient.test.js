import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildCaseReportPayload,
  buildForensicTimelineQuery,
} from '../src/api/forensicPayload.js'
import {
  forensicReportState,
  normalizeForensicTimeline,
  redactSensitiveText,
  sourceCitationText,
} from '../src/pages/casesForensics.js'

test('getForensicTimeline encodes bounds and clamps the request limit', async () => {
  assert.equal(
    buildForensicTimelineQuery({
    startAt: '2026-03-01T00:00:00Z',
    endAt: '2026-03-02T00:00:00Z',
    limit: 99999,
    }),
    '?start_at=2026-03-01T00%3A00%3A00Z&end_at=2026-03-02T00%3A00%3A00Z&limit=5000',
  )
})

test('generateCaseReport sends the backend field names and model default', () => {
  assert.deepEqual(buildCaseReportPayload({
    question: 'What should I review?',
    allowModel: true,
    limit: 4,
  }), {
    question: 'What should I review?',
    allow_model: true,
    limit: 4,
  })
})

test('forensic UI state distinguishes empty, loading, error, and ready reports', () => {
  assert.equal(forensicReportState(), 'empty')
  assert.equal(forensicReportState({ loading: true }), 'loading')
  assert.equal(forensicReportState({ error: 'backend unavailable' }), 'error')
  assert.equal(forensicReportState({ report: { report: {} } }), 'ready')
})

test('forensic presentation redacts secret-shaped values and preserves citations', () => {
  const fakeGeminiKey = ['AIza', '123456789012345678901234567890'].join('')
  assert.equal(
    redactSensitiveText(`api_key=${fakeGeminiKey}`),
    'api_key: [redacted]',
  )
  assert.equal(sourceCitationText([1, 3]), '[1] [3]')
  assert.equal(sourceCitationText([]), 'no source citation')
  assert.deepEqual(normalizeForensicTimeline({ entries: [{ id: 'entry-1' }] }).entries, [{ id: 'entry-1' }])
})
