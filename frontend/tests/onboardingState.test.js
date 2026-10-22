import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ONBOARDING_STEP_FOLDER_SELECT,
  ONBOARDING_STEP_WELCOME,
  isRestoringExistingLibraryStatus,
  isSearchReadyWhileFinishingStatus,
  parseStoredOnboardingStep,
  serializeOnboardingStep,
} from '../src/pages/onboardingState.js'

test('stored onboarding state only resumes folder selection', () => {
  assert.equal(parseStoredOnboardingStep('folder_select'), ONBOARDING_STEP_FOLDER_SELECT)
  assert.equal(parseStoredOnboardingStep('scanning'), ONBOARDING_STEP_WELCOME)
  assert.equal(parseStoredOnboardingStep(null), ONBOARDING_STEP_WELCOME)
})

test('only folder selection is persisted to storage', () => {
  assert.equal(serializeOnboardingStep(ONBOARDING_STEP_FOLDER_SELECT), 'folder_select')
  assert.equal(serializeOnboardingStep(2), null)
  assert.equal(serializeOnboardingStep(0), null)
})

test('restoring state matches the stuck existing-library scan state', () => {
  assert.equal(
    isRestoringExistingLibraryStatus({
      isScanning: true,
      pendingFiles: 0,
      queueSize: 0,
      phaseFound: 0,
      phaseChecked: 0,
      phaseTotal: 0,
      phaseNewFound: 0,
      backendProcessed: 0,
      backendTotal: 0,
      uniqueMemories: 1511,
      filesWatched: 1511,
    }),
    true,
  )
})

test('restoring state stays false when there is real active scan work', () => {
  assert.equal(
    isRestoringExistingLibraryStatus({
      isScanning: true,
      pendingFiles: 0,
      queueSize: 0,
      phaseFound: 120,
      phaseChecked: 0,
      phaseTotal: 0,
      phaseNewFound: 0,
      backendProcessed: 0,
      backendTotal: 0,
      uniqueMemories: 1511,
      filesWatched: 1511,
    }),
    false,
  )
})

test('search-ready finishing state is treated as non-blocking', () => {
  assert.equal(
    isSearchReadyWhileFinishingStatus({
      isScanning: false,
      pendingFiles: 19,
      queueSize: 0,
      searchableNow: true,
    }),
    true,
  )
})

test('search-ready finishing state stays false until search is actually ready', () => {
  assert.equal(
    isSearchReadyWhileFinishingStatus({
      isScanning: false,
      pendingFiles: 19,
      queueSize: 0,
      searchableNow: false,
    }),
    false,
  )
})
