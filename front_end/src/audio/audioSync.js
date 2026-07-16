const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

export const TRACK_SOFT_SYNC_THRESHOLD = 0.005
export const TRACK_HARD_SYNC_THRESHOLD = 0.03
export const TRACK_MAX_RATE_DELTA = 0.01
const HARD_SYNC_COOLDOWN_MS = 180
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
    if (Math.abs(follower.playbackRate - baseRate) > 0.001) {
      follower.playbackRate = baseRate
    }
    return absDrift
  }

  if (absDrift >= TRACK_HARD_SYNC_THRESHOLD) {
    const now = performance.now()
    const lastSync = lastHardSyncAt.get(follower) || 0
    if (!follower.seeking && now - lastSync >= HARD_SYNC_COOLDOWN_MS) {
      seekMedia(follower, master.currentTime)
      lastHardSyncAt.set(follower, now)
    }
    follower.playbackRate = baseRate
    return absDrift
  }

  const correction = clamp(absDrift * 0.35, 0.002, TRACK_MAX_RATE_DELTA)
  follower.playbackRate = clamp(
    drift > 0 ? baseRate + correction : baseRate - correction,
    baseRate - TRACK_MAX_RATE_DELTA,
    baseRate + TRACK_MAX_RATE_DELTA
  )
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
