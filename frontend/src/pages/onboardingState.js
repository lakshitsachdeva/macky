export const ONBOARDING_STEP_WELCOME = 0
export const ONBOARDING_STEP_FOLDER_SELECT = 1
export const ONBOARDING_STEP_SCANNING = 2

export function parseStoredOnboardingStep(value) {
  if (value === 'folder_select') {
    return ONBOARDING_STEP_FOLDER_SELECT
  }

  return ONBOARDING_STEP_WELCOME
}

export function serializeOnboardingStep(step) {
  if (step === ONBOARDING_STEP_FOLDER_SELECT) {
    return 'folder_select'
  }

  return null
}

export function isRestoringExistingLibraryStatus({
  uiTestMode = false,
  isScanning = false,
  pendingFiles = 0,
  queueSize = 0,
  phaseFound = 0,
  phaseChecked = 0,
  phaseTotal = 0,
  phaseNewFound = 0,
  backendProcessed = 0,
  backendTotal = 0,
  uniqueMemories = 0,
  filesWatched = 0,
} = {}) {
  if (uiTestMode || !isScanning) {
    return false
  }

  if (pendingFiles > 0 || queueSize > 0) {
    return false
  }

  if (phaseFound > 0 || phaseChecked > 0 || phaseTotal > 0 || phaseNewFound > 0) {
    return false
  }

  if (backendProcessed > 0 || backendTotal > 0) {
    return false
  }

  return uniqueMemories > 0 && filesWatched > 0
}

export function isSearchReadyWhileFinishingStatus({
  uiTestMode = false,
  isScanning = false,
  pendingFiles = 0,
  queueSize = 0,
  searchableNow = false,
} = {}) {
  if (uiTestMode || isScanning) {
    return false
  }

  if (pendingFiles <= 0 && queueSize <= 0) {
    return false
  }

  return searchableNow
}
