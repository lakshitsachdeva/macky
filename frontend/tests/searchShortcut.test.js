import test from 'node:test'
import assert from 'node:assert/strict'

import { isSearchShortcut } from '../src/utils/keyboard.js'

test('local search shortcut accepts the mac and control modifier variants', () => {
  assert.equal(isSearchShortcut({ metaKey: true, shiftKey: true, key: ' ', code: 'Space' }), true)
  assert.equal(isSearchShortcut({ ctrlKey: true, shiftKey: true, key: ' ', code: 'Space' }), true)
})

test('local search shortcut ignores unrelated or repeated key presses', () => {
  assert.equal(isSearchShortcut({ metaKey: true, shiftKey: true, key: 'k', code: 'KeyK' }), false)
  assert.equal(isSearchShortcut({ metaKey: true, shiftKey: true, altKey: true, key: ' ', code: 'Space' }), false)
  assert.equal(isSearchShortcut({ metaKey: true, shiftKey: true, repeat: true, key: ' ', code: 'Space' }), false)
})
