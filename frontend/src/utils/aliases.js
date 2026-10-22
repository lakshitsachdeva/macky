/**
 * Expand user-defined aliases in a query string.
 * Aliases: { "athair": "Athair AI captcha baseline", "uni": "university project" }
 * Word-boundary match, case-insensitive.
 */
export function expandAliases(query, aliases) {
  if (!aliases || typeof aliases !== 'object') return query
  let result = query
  for (const [name, expansion] of Object.entries(aliases)) {
    if (!name || !expansion) continue
    const re = new RegExp(`\\b${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'gi')
    result = result.replace(re, expansion)
  }
  return result
}
