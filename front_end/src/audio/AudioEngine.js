import { SoundTouchNode } from '@soundtouchjs/audio-worklet'
import processorUrl from '@soundtouchjs/audio-worklet/processor?url'

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

const rampGain = (gainNode, value, duration = 0.02) => {
  const now = gainNode.context.currentTime
  const target = clamp(Number(value) || 0, 0, 1)
  gainNode.gain.cancelScheduledValues(now)
  gainNode.gain.setValueAtTime(gainNode.gain.value, now)
  gainNode.gain.linearRampToValueAtTime(target, now + duration)
}

/**
 * Shared two-track Web Audio graph.
 *
 * Both tracks are mixed before SoundTouch, so they receive identical DSP
 * latency and only one high-quality pitch processor is required.
 */
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
    this.outputMuted = false
    this.initialized = false
    this.initPromise = null
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
    if (!this.context.audioWorklet) {
      await this.context.close()
      this.context = null
      throw new Error('AudioWorklet requires localhost or HTTPS')
    }

    // Register before creating MediaElementSourceNodes. If registration fails,
    // normal element playback remains available as a clean original-key fallback.
    await SoundTouchNode.register(this.context, processorUrl)

    this.originalSource = this.context.createMediaElementSource(this.originalElement)
    this.instrumentalSource = this.context.createMediaElementSource(this.instrumentalElement)
    this.originalGain = this.context.createGain()
    this.instrumentalGain = this.context.createGain()
    this.mixBus = this.context.createGain()
    this.dryGain = this.context.createGain()
    this.wetGain = this.context.createGain()
    this.pitchShift = new SoundTouchNode({
      context: this.context,
      interpolationStrategy: 'lanczos',
      outputChannelCount: 2
    })

    // Quality-first WSOLA settings for full music rather than voice effects.
    this.pitchShift.setStretchParameters({
      sequenceMs: 80,
      seekWindowMs: 20,
      overlapMs: 12,
      quickSeek: false
    })
    this.pitchShift.pitch.value = 1
    this.pitchShift.pitchSemitones.value = 0
    this.pitchShift.playbackRate.value = 1

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
    this.mixBus.connect(this.pitchShift)
    this.pitchShift.connect(this.wetGain)
    this.wetGain.connect(this.context.destination)

    this.originalGain.gain.value = 1
    this.instrumentalGain.gain.value = 0.001
    this.dryGain.gain.value = 1
    this.wetGain.gain.value = 0
    this.initialized = true
    return true
  }

  async resume() {
    if (!this.initialized) return false
    if (this.context.state !== 'running') await this.context.resume()
    return this.context.state === 'running'
  }

  setTrackGains(original, instrumental, immediate = false) {
    if (!this.initialized) return
    const duration = immediate ? 0.001 : 0.02
    rampGain(this.originalGain, original, duration)
    rampGain(this.instrumentalGain, instrumental, duration)
  }

  setOutputMuted(muted) {
    if (!this.initialized) return
    this.outputMuted = !!muted
    const now = this.context.currentTime
    const dryValue = this.outputMuted || this.pitchEnabled ? 0 : 1
    const wetValue = this.outputMuted || !this.pitchEnabled ? 0 : 1

    this.dryGain.gain.cancelScheduledValues(now)
    this.wetGain.gain.cancelScheduledValues(now)
    this.dryGain.gain.setValueAtTime(dryValue, now)
    this.wetGain.gain.setValueAtTime(wetValue, now)
  }

  setPitch(semitones, enabled) {
    if (!this.initialized) return
    const value = clamp(Math.round(Number(semitones) || 0), -6, 6)
    this.pitchEnabled = !!enabled
    const now = this.context.currentTime
    this.pitchShift.pitchSemitones.cancelScheduledValues(now)
    this.pitchShift.pitchSemitones.setValueAtTime(value, now)
    if (this.outputMuted) return
    rampGain(this.dryGain, this.pitchEnabled ? 0 : 1)
    rampGain(this.wetGain, this.pitchEnabled ? 1 : 0)
  }

  dispose() {
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
    this.pitchShift.disconnect()
    if (this.context.state !== 'closed') this.context.close().catch(() => {})
    this.initialized = false
  }
}
