const QUERY_CACHE_MAX = 20

const queryCache = new Map()
let cacheVersion = 0

function hasSearchResults(value) {
  return Array.isArray(value?.results) && value.results.length > 0
}

export function getQueryCacheVersion() {
  return cacheVersion
}

export function queryCacheGet(key, expectedVersion = cacheVersion) {
  if (expectedVersion !== cacheVersion || !queryCache.has(key)) return null

  const value = queryCache.get(key)
  queryCache.delete(key)
  queryCache.set(key, value)
  return value
}

export function queryCacheSet(key, value, expectedVersion = cacheVersion) {
  // A response that started before an indexing event must not repopulate the
  // cache with a pre-index snapshot.
  if (expectedVersion !== cacheVersion || !hasSearchResults(value)) return false

  if (queryCache.has(key)) queryCache.delete(key)
  else if (queryCache.size >= QUERY_CACHE_MAX) queryCache.delete(queryCache.keys().next().value)
  queryCache.set(key, value)
  return true
}

export function invalidateQueryCache() {
  cacheVersion += 1
  queryCache.clear()
  return cacheVersion
}
