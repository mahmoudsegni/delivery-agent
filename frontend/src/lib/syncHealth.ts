/** Mirrors backend config sync_stale_hours (default 6) — keep in sync. */
export const STALE_SYNC_HOURS = 6;

/** True when the board is lying: last sync errored, or older than the stall
 * threshold. Boundary is strict: exactly 6h old is still fresh. */
export function syncStale(lastSyncAt: string | null | undefined, lastStatus: string | null | undefined, now = new Date()): boolean {
  if (lastStatus === 'error') return true;
  if (!lastSyncAt) return false; // demo mode / never synced: no signal, no noise
  return now.getTime() - new Date(lastSyncAt).getTime() > STALE_SYNC_HOURS * 3600_000;
}
