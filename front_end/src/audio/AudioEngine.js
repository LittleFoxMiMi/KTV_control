import { SoundTouchNode } from '@soundtouchjs/audio-worklet'
import highQualityProcessorUrl from '@soundtouchjs/audio-worklet/processor?url'
import lowCostProcessorUrl from './lowCostSoundTouchProcessor.js?worker&url'

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))
const useHighQualityProfile = /Windows/i.test(navigator.userAgent)
const PITCH_CROSSFADE_SECONDS = 0.025
const PITCH_WARMUP_MS = 60

const setGain = (gainNode, value, duration = PITCH_CROSSFADE_SECONDS) => {
  if (!gainNode) return
  const now = gainNode.context.currentTime
  const target = clamp(Number(value) || 0, 0, 1)
  gainNode.gain.cancelScheduledValues(now)
  gainNode.gain.setValueAtTime(gainNode.gain.value, now)
  gainNode.gain.linearRampToValueAtTime(target, now + duration)
}

/** Shared two-track graph with a pitch processor that is connected on demand. */
export class AudioEngine {
  constructor(originalElement, instrumentalElement) {
    this.originalElement = originalElement
    this.instrumentalElement = instrumentalElement
    this.context = null
    this.originalSource = null
    this.instrumentalSource = null
    this.originalGain = null
    this.instrumentalGain = null
    this.mixBus = null
    this.dryGain = null
    this.wetGain = null
    this.pitchShift = null
    this.pitchEnabled = false
    this.pitchConnected = false
    this.pitchInitPromise = null
    this.pitchTransitionToken = 0
    this.outputMuted = false
    this.initialized = false
    this.initPromise = null
    this.disposed = false
  }

  async init() {
    if (this.initialized) return true
    if (this.initPromise) return this.initPromise
    if (!this.originalElement || !this.instrumentalElement) return false

    this.initPromise = this.initializeGraph()
    try {
      return await this.initPromise
    } finally {
      this.initPromise = null
    }
  }

  async initializeGraph() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext
    if (!AudioContextClass) throw new Error('Web Audio is not supported by this browser')

    this.context = new AudioContextClass({ latencyHint: 'playback' })
    this.originalSource = this.context.createMediaElementSource(this.originalElement)
    this.instrumentalSource = this.context.createMediaElementSource(this.instrumentalElement)
    this.originalGain = this.context.createGain()
    this.instrumentalGain = this.context.createGain()
    this.mixBus = this.context.createGain()
    this.dryGain = this.context.createGain()
    this.wetGain = this.context.createGain()

    this.originalElement.muted = false
    this.instrumentalElement.muted = false
    this.originalElement.volume = 1
    this.instrumentalElement.volume = 1
    this.originalElement.preservesPitch = true
    this.instrumentalElement.preservesPitch = true

    this.originalSource.connect(this.originalGain)
    this.instrumentalSource.connect(this.instrumentalGain)
    this.originalGain.connect(this.mixBus)
    this.instrumentalGain.connect(this.mixBus)
    this.mixBus.connect(this.dryGain)
    this.dryGain.connect(this.context.destination)
    this.wetGain.connect(this.context.destination)

    this.originalGain.gain.value = 1
    this.instrumentalGain.gain.value = 0.001
    this.dryGain.gain.value = 1
    this.wetGain.gain.value = 0
    this.initialized = true
    return true
  }

  async ensurePitchProcessor() {
    if (this.pitchShift) return this.pitchShift
    if (this.pitchInitPromise) return this.pitchInitPromise
    if (!this.context?.audioWorklet) throw new Error('Pitch shifting requires HTTPS or localhost')

    this.pitchInitPromise = (async () => {
      const processorUrl = useHighQualityProfile ? highQualityProcessorUrl : lowCostProcessorUrl
      await SoundTouchNode.register(this.context, processorUrl)
      if (this.disposed) return null

      const pitchShift = new SoundTouchNode({
        context: this.context,
        interpolationStrategy: useHighQualityProfile ? 'lanczos' : 'linear',
        outputChannelCount: 2
      })
      if (useHighQualityProfile) {
        pitchShift.setStretchParameters({
          sequenceMs: 80,
          seekWindowMs: 20,
          overlapMs: 12,
          quickSeek: false
        })
      } else {
        pitchShift.setInterpolationStrategyParams({ edgeHoldFrames: 1, blend: 1 })
        pitchShift.setStretchParameters({
          sequenceMs: 40,
          seekWindowMs: 10,
          overlapMs: 6,
          quickSeek: true
        })
      }
      pitchShift.pitch.value = 1
      pitchShift.pitchSemitones.value = 0
      pitchShift.playbackRate.value = 1
      this.pitchShift = pitchShift
      return pitchShift
    })()

    try {
      return await this.pitchInitPromise
    } finally {
      this.pitchInitPromise = null
    }
  }

  async resume() {
    if (!this.initialized) return false
    if (this.context.state !== 'running') await this.context.resume()
    return this.context.state === 'running'
  }

  setTrackGains(original, instrumental, immediate = false) {
    if (!this.initialized) return
    const duration = immediate ? 0.001 : 0.02
    setGain(this.originalGain, original, duration)
    setGain(this.instrumentalGain, instrumental, duration)
  }

  setOutputMuted(muted) {
    if (!this.initialized) return
    this.outputMuted = !!muted
    const pitchActive = this.pitchEnabled && this.pitchConnected
    setGain(this.dryGain, this.outputMuted || pitchActive ? 0 : 1, 0.001)
    setGain(this.wetGain, this.outputMuted || !pitchActive ? 0 : 1, 0.001)
  }

  setPitch(semitones, enabled) {
    if (!this.initialized) return Promise.resolve(false)
    const value = clamp(Math.round(Number(semitones) || 0), -6, 6)
    const shouldEnable = !!enabled && value !== 0
    this.pitchEnabled = shouldEnable
    const token = ++this.pitchTransitionToken

    if (!shouldEnable) {
      this.disablePitch(token)
      return Promise.resolve(true)
    }

    return this.enablePitch(value, token).catch((error) => {
      if (token === this.pitchTransitionToken) {
        this.pitchEnabled = false
        this.disablePitch(token)
      }
      console.error('Pitch shift unavailable; using unprocessed audio', error)
      return false
    })
  }

  async enablePitch(semitones, token) {
    const pitchShift = await this.ensurePitchProcessor()
    if (!pitchShift || token !== this.pitchTransitionToken || !this.pitchEnabled || this.disposed) return false

    const now = this.context.currentTime
    pitchShift.pitchSemitones.cancelScheduledValues(now)
    pitchShift.pitchSemitones.setValueAtTime(semitones, now)
    if (!this.pitchConnected) {
      this.mixBus.connect(pitchShift)
      pitchShift.connect(this.wetGain)
      this.pitchConnected = true
      await new Promise((resolve) => setTimeout(resolve, PITCH_WARMUP_MS))
    }

    if (token !== this.pitchTransitionToken || !this.pitchEnabled || this.disposed) return false
    if (!this.outputMuted) {
      setGain(this.dryGain, 0)
      setGain(this.wetGain, 1)
    }
    return true
  }

  disablePitch(token) {
    if (!this.initialized) return
    if (!this.outputMuted) {
      setGain(this.dryGain, 1)
      setGain(this.wetGain, 0)
    } else {
      setGain(this.dryGain, 0, 0.001)
      setGain(this.wetGain, 0, 0.001)
    }
    if (!this.pitchConnected || !this.pitchShift) return

    setTimeout(() => {
      if (token !== this.pitchTransitionToken || this.pitchEnabled || !this.pitchConnected) return
      try {
        this.mixBus.disconnect(this.pitchShift)
        this.pitchShift.disconnect(this.wetGain)
      } catch (error) {
        // The graph may already be disconnected during teardown.
      }
      this.pitchConnected = false
    }, Math.ceil(PITCH_CROSSFADE_SECONDS * 1000) + 5)
  }

  dispose() {
    this.disposed = true
    this.pitchTransitionToken += 1
    if (!this.initialized) {
      if (this.context && this.context.state !== 'closed') this.context.close().catch(() => {})
      return
    }
    this.originalSource.disconnect()
    this.instrumentalSource.disconnect()
    this.originalGain.disconnect()
    this.instrumentalGain.disconnect()
    this.mixBus.disconnect()
    this.dryGain.disconnect()
    this.wetGain.disconnect()
    this.pitchShift?.disconnect()
    if (this.context.state !== 'closed') this.context.close().catch(() => {})
    this.initialized = false
    this.pitchShift = null
    this.pitchConnected = false
  }
}
