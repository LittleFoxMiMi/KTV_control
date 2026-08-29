export const PLAYBACK_STATE = Object.freeze({
  IDLE: 'idle',
  LOADING: 'loading',
  PLAYING: 'playing',
  PAUSED: 'paused',
  WAITING: 'waiting',
  SEEKING: 'seeking',
  BLOCKED: 'blocked',
  DISPOSED: 'disposed'
})

const defaultIsAutoplayBlocked = (error) => {
  const name = error?.name || ''
  return name === 'NotAllowedError' || name === 'NotSupportedError' || /NotAllowedError/i.test(String(error))
}

export class PlaybackController {
  constructor({
    master,
    followers = [],
    synchronize,
    measureDrift,
    resumeOutput,
    onBlocked,
    isAutoplayBlocked = defaultIsAutoplayBlocked,
    waitingGraceMs = 500,
    healthCheckMs = 1000,
    driftThreshold = 1,
    driftSamplesRequired = 1
  }) {
    this.master = master
    this.followers = followers.filter(Boolean)
    this.synchronize = synchronize
    this.measureDrift = measureDrift
    this.resumeOutput = resumeOutput
    this.onBlocked = onBlocked
    this.isAutoplayBlocked = isAutoplayBlocked
    this.waitingGraceMs = waitingGraceMs
    this.healthCheckMs = healthCheckMs
    this.driftThreshold = driftThreshold
    this.driftSamplesRequired = driftSamplesRequired
    this.state = PLAYBACK_STATE.IDLE
    this.waitingTimer = null
    this.followersPausedForWaiting = false
    this.pendingMasterPlay = null
    this.pendingFollowerPlay = new WeakMap()
    this.lastHealthCheckAt = 0
    this.driftSamples = 0
    this.attached = false

    this.onMasterPlay = () => this.activateFollowers('play')
    this.onMasterPlaying = () => this.handlePlaying()
    this.onMasterPause = () => this.handlePause()
    this.onMasterWaiting = () => this.handleWaiting()
    this.onMasterSeeking = () => this.handleSeeking()
    this.onMasterSeeked = () => this.handleSeeked()
    this.onMasterTimeUpdate = () => this.handleTimeUpdate()
    this.onMasterEnded = () => this.handleEnded()
    this.onMasterRateChange = () => this.syncNow('ratechange', true)
  }

  attach() {
    if (this.attached || !this.master) return
    this.master.addEventListener('play', this.onMasterPlay)
    this.master.addEventListener('playing', this.onMasterPlaying)
    this.master.addEventListener('pause', this.onMasterPause)
    this.master.addEventListener('waiting', this.onMasterWaiting)
    this.master.addEventListener('seeking', this.onMasterSeeking)
    this.master.addEventListener('seeked', this.onMasterSeeked)
    this.master.addEventListener('timeupdate', this.onMasterTimeUpdate)
    this.master.addEventListener('ended', this.onMasterEnded)
    this.master.addEventListener('ratechange', this.onMasterRateChange)
    this.attached = true
    this.state = this.master.paused ? PLAYBACK_STATE.PAUSED : PLAYBACK_STATE.PLAYING
  }

  beginLoading() {
    this.clearWaitingTimer()
    this.followersPausedForWaiting = false
    this.resetDriftGuard()
    this.pauseFollowers()
    if (this.master && !this.master.paused) this.master.pause()
    this.state = PLAYBACK_STATE.LOADING
  }

  async ready({ autoplay = false } = {}) {
    if (this.state === PLAYBACK_STATE.DISPOSED) return false
    this.state = PLAYBACK_STATE.PAUSED
    this.syncNow('ready', true)
    if (!autoplay) return true
    return this.play(true)
  }

  async play(requireGesture = true) {
    if (!this.master || this.state === PLAYBACK_STATE.DISPOSED || this.state === PLAYBACK_STATE.LOADING) {
      return false
    }
    if (this.pendingMasterPlay) return this.pendingMasterPlay

    const playPromise = this.startMaster(requireGesture)
    this.pendingMasterPlay = playPromise
    try {
      return await playPromise
    } finally {
      if (this.pendingMasterPlay === playPromise) this.pendingMasterPlay = null
    }
  }

  async startMaster(requireGesture) {
    try {
      const running = await this.resumeOutput?.()
      if (running === false) throw new DOMException('Audio output could not start', 'NotAllowedError')
      this.syncNow('play-command', true)
      await this.master.play()
      this.state = PLAYBACK_STATE.PLAYING
      await this.activateFollowers('play-command')
      return true
    } catch (error) {
      if (requireGesture && this.isAutoplayBlocked(error)) this.block(error)
      return false
    }
  }

  pause() {
    if (this.state === PLAYBACK_STATE.DISPOSED) return
    this.clearWaitingTimer()
    if (this.master && !this.master.paused) this.master.pause()
    this.pauseFollowers()
    if (this.state !== PLAYBACK_STATE.LOADING) this.state = PLAYBACK_STATE.PAUSED
  }

  toggle() {
    if (!this.master) return
    if (this.master.paused) this.play(true)
    else this.pause()
  }

  restart() {
    if (!this.master || this.state === PLAYBACK_STATE.DISPOSED) return
    this.master.currentTime = 0
    this.resetDriftGuard()
    this.syncNow('restart', true)
    this.play(true)
  }

  unblock() {
    if (this.state === PLAYBACK_STATE.BLOCKED) this.state = PLAYBACK_STATE.PAUSED
  }

  syncNow(reason = 'external', force = true) {
    if (this.state === PLAYBACK_STATE.DISPOSED) return false
    return this.synchronize?.({ force, reason }) !== false
  }

  async activateFollowers(reason) {
    if (!this.master || this.master.paused || this.state === PLAYBACK_STATE.LOADING || this.state === PLAYBACK_STATE.BLOCKED) {
      return false
    }

    const shouldPlayFollowers = this.syncNow(reason, false)
    if (!shouldPlayFollowers) {
      this.pauseFollowers()
      return false
    }

    const results = await Promise.all(this.followers.map((follower) => this.playFollower(follower)))
    if (results.some((result) => !result)) return false
    if (this.state !== PLAYBACK_STATE.WAITING && this.state !== PLAYBACK_STATE.SEEKING) {
      this.state = PLAYBACK_STATE.PLAYING
    }
    return true
  }

  async playFollower(follower) {
    if (!follower || !follower.paused) return true
    const existing = this.pendingFollowerPlay.get(follower)
    if (existing) return existing

    const playPromise = follower.play()
      .then(() => true)
      .catch((error) => {
        if (this.isAutoplayBlocked(error)) this.block(error)
        return false
      })
      .finally(() => {
        if (this.pendingFollowerPlay.get(follower) === playPromise) {
          this.pendingFollowerPlay.delete(follower)
        }
      })
    this.pendingFollowerPlay.set(follower, playPromise)
    return playPromise
  }

  pauseFollowers() {
    for (const follower of this.followers) {
      if (!follower.paused) follower.pause()
    }
  }

  handlePlaying() {
    const shouldRealign = this.followersPausedForWaiting
    this.followersPausedForWaiting = false
    this.clearWaitingTimer()
    if (this.state !== PLAYBACK_STATE.LOADING && this.state !== PLAYBACK_STATE.BLOCKED) {
      this.state = PLAYBACK_STATE.PLAYING
      if (shouldRealign) this.syncNow('waiting-recovered', true)
      this.activateFollowers('playing')
    }
  }

  handlePause() {
    this.pauseFollowers()
    if (![PLAYBACK_STATE.LOADING, PLAYBACK_STATE.SEEKING, PLAYBACK_STATE.BLOCKED].includes(this.state)) {
      this.state = PLAYBACK_STATE.PAUSED
    }
  }

  handleWaiting() {
    if (this.state === PLAYBACK_STATE.LOADING || this.state === PLAYBACK_STATE.BLOCKED) return
    this.state = PLAYBACK_STATE.WAITING
    if (this.waitingTimer) return
    this.waitingTimer = setTimeout(() => {
      this.waitingTimer = null
      if (this.state === PLAYBACK_STATE.WAITING) {
        this.followersPausedForWaiting = true
        this.pauseFollowers()
      }
    }, this.waitingGraceMs)
  }

  handleSeeking() {
    if (this.state === PLAYBACK_STATE.LOADING) return
    this.clearWaitingTimer()
    this.state = PLAYBACK_STATE.SEEKING
    this.pauseFollowers()
    this.resetDriftGuard()
  }

  handleSeeked() {
    if (this.state === PLAYBACK_STATE.LOADING) return
    this.syncNow('seeked', true)
    if (this.master.paused) {
      this.state = PLAYBACK_STATE.PAUSED
    } else {
      this.state = PLAYBACK_STATE.PLAYING
      this.activateFollowers('seeked')
    }
  }

  handleTimeUpdate() {
    if (this.state !== PLAYBACK_STATE.PLAYING || !this.master || this.master.paused) return

    // timeupdate is generated by the media pipeline itself, so no independent
    // timer keeps waking weak devices while playback is stable.
    const shouldPlayFollowers = this.syncNow('timeupdate', false)
    if (shouldPlayFollowers && this.followers.some((follower) => follower.paused)) {
      this.activateFollowers('timeupdate')
    }

    const now = performance.now()
    if (!this.measureDrift || now - this.lastHealthCheckAt < this.healthCheckMs) return
    this.lastHealthCheckAt = now
    const drift = Number(this.measureDrift()) || 0
    this.driftSamples = drift > this.driftThreshold ? this.driftSamples + 1 : 0
    if (this.driftSamples < this.driftSamplesRequired) return
    this.resetDriftGuard()
    this.syncNow('sustained-drift', true)
  }

  handleEnded() {
    this.clearWaitingTimer()
    this.pauseFollowers()
    this.state = PLAYBACK_STATE.PAUSED
  }

  block(error) {
    this.clearWaitingTimer()
    this.pauseFollowers()
    if (this.master && !this.master.paused) this.master.pause()
    this.state = PLAYBACK_STATE.BLOCKED
    this.onBlocked?.(error)
  }

  resetDriftGuard() {
    this.lastHealthCheckAt = 0
    this.driftSamples = 0
  }

  clearWaitingTimer() {
    if (!this.waitingTimer) return
    clearTimeout(this.waitingTimer)
    this.waitingTimer = null
  }

  dispose() {
    if (this.state === PLAYBACK_STATE.DISPOSED) return
    this.clearWaitingTimer()
    this.pauseFollowers()
    if (this.attached && this.master) {
      this.master.removeEventListener('play', this.onMasterPlay)
      this.master.removeEventListener('playing', this.onMasterPlaying)
      this.master.removeEventListener('pause', this.onMasterPause)
      this.master.removeEventListener('waiting', this.onMasterWaiting)
      this.master.removeEventListener('seeking', this.onMasterSeeking)
      this.master.removeEventListener('seeked', this.onMasterSeeked)
      this.master.removeEventListener('timeupdate', this.onMasterTimeUpdate)
      this.master.removeEventListener('ended', this.onMasterEnded)
      this.master.removeEventListener('ratechange', this.onMasterRateChange)
    }
    this.attached = false
    this.state = PLAYBACK_STATE.DISPOSED
  }
}
