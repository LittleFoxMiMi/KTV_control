// Mobile browsers commonly expose media clocks with several milliseconds of
// jitter. Treat that as normal instead of continuously correcting the track.
export const TRACK_SOFT_SYNC_THRESHOLD = 0.04
export const TRACK_HARD_SYNC_THRESHOLD = 0.25
export const TRACK_MAX_RATE_DELTA = 0
const HARD_SYNC_COOLDOWN_MS = 1500
const lastHardSyncAt = new WeakMap()

export const seekMedia = (element, time) => {
  if (!element) return
  const safeTime = Math.max(0, Number(time) || 0)
  // currentTime is intentionally used instead of fastSeek: fastSeek may snap
  // to an approximate decode point, which is unsuitable for separated tracks.
  element.currentTime = safeTime
}

/**
 * Keep one follower track aligned to a fixed master clock. The master is never
 * rate-corrected, which prevents the two audio elements from chasing each other.
 */
export const syncFollowerToMaster = (master, follower, baseRate = 1) => {
  if (!master || !follower) return 0

  const drift = master.currentTime - follower.currentTime
  const absDrift = Math.abs(drift)

  if (master.paused || absDrift <= TRACK_SOFT_SYNC_THRESHOLD) {
    return absDrift
  }

  if (absDrift >= TRACK_HARD_SYNC_THRESHOLD) {
    const now = performance.now()
    const lastSync = lastHardSyncAt.get(follower) || 0
    if (!follower.seeking && now - lastSync >= HARD_SYNC_COOLDOWN_MS) {
      seekMedia(follower, master.currentTime)
      lastHardSyncAt.set(follower, now)
    }
    if (Math.abs(follower.playbackRate - baseRate) > 0.01) {
      follower.playbackRate = baseRate
    }
    return absDrift
  }

  // Do not use playbackRate as a continuous servo. On mobile devices that
  // often invokes additional resampling and is audible as stutter.
  return absDrift
}

export const hardAlignTracks = (master, follower, time, baseRate = 1) => {
  seekMedia(master, time)
  seekMedia(follower, time)
  if (master) master.playbackRate = baseRate
  if (follower) {
    follower.playbackRate = baseRate
    lastHardSyncAt.set(follower, performance.now())
  }
}
