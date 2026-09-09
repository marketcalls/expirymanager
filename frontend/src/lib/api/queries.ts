// Re-export surface for the query key factory.
//
// ARCHITECTURE.md section 8 names this file and BUILD-PLAN.md names lib/api/keys.ts. They are
// the same thing: the keys live in keys.ts, and this module exists so an import written against
// either document resolves. There are deliberately no shared query functions here. Each route
// builds its own useQuery from the keys, so four screens fetching contracts with four different
// filter sets never collide on one shared function signature.

export { queryKeys, PINNED_KEY_PREFIXES } from '@/lib/api/keys'
export type { KeyParams } from '@/lib/api/keys'
