<template>
  <div class="ktv-player">
    <video
      ref="videoRef"
      class="main-video"
      playsinline
      controls
      crossorigin
      :src="videoSrc"
    ></video>

    <div v-if="needsGesture" class="gesture-overlay" @click="unlockAudio">
        点击屏幕启用声音
    </div>
    
    <!-- Original Audio Track -->
    <audio ref="origAudioRef" preload="auto" crossorigin :src="originalAudioSrc"></audio>
    
    <!-- Instrumental Audio Track -->
    <audio ref="instAudioRef" preload="auto" crossorigin :src="audioSrc"></audio>

    <div class="controls-ui" v-if="showControls">
        <div class="control-group">
            <label>Mode:</label>
            <button @click="setTrack('original')" :class="{active: trackMode==='original'}">Original</button>
            <button @click="setTrack('instrumental')" :class="{active: trackMode==='instrumental'}">Instrumental</button>
            <button @click="setTrack('mix')" :class="{active: trackMode==='mix'}">Mix</button>
            <button @click="toggleDelay" :class="{active: delayEnabled}">Delay</button>
        </div>

        <div class="control-group">
            <label>Delay (Global): {{ delayEnabled ? delay : 0 }}ms</label>
            <input type="range" min="-2000" max="2000" step="50" v-model.number="delay" :disabled="!delayEnabled" @input="onDelayInput">
        </div>
        
         <!-- Volume Controls based on Mode -->
         <div v-if="trackMode==='original'" class="control-group">
             <label for="vol-orig-solo">Vocals:</label>
             <input id="vol-orig-solo" type="range" min="0" max="100" v-model.number="volOrigSolo" @input="updateVolume">
         </div>

         <div v-if="trackMode==='instrumental'" class="control-group">
             <label for="vol-inst-solo">Inst:</label>
             <input id="vol-inst-solo" type="range" min="0" max="100" v-model.number="volInstSolo" @input="updateVolume">
         </div>

         <div v-if="trackMode==='mix'" class="control-group">
             <label for="vol-orig-mix">Vocals:</label>
             <input id="vol-orig-mix" type="range" min="0" max="100" v-model.number="volOrigMix" @input="updateVolume">
             <label for="vol-inst-mix">Inst:</label>
             <input id="vol-inst-mix" type="range" min="0" max="100" v-model.number="volInstMix" @input="updateVolume">
         </div>
    </div>
    
    <!-- Track Change Toast -->
    <transition name="fade">
        <div v-if="showToast" class="track-toast">
            Currently playing: {{ toastMessage }}
        </div>
    </transition>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import Plyr from 'plyr'
import axios from 'axios'

// API Base
const getHost = () => window.location.hostname
const API_BASE = `http://${getHost()}:8000`

const props = defineProps({
    videoSrc: String,
    audioSrc: String,         // Instrumental
    originalAudioSrc: String,  // Raw Audio
    showControls: {
        type: Boolean,
        default: true
    }
})

const videoRef = ref(null)
const origAudioRef = ref(null)
const instAudioRef = ref(null)
const player = ref(null)
const needsGesture = ref(false)

const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
const SYNC_CHECK_INTERVAL_MS = isMobile ? 500 : 350
const SOFT_SYNC_MIN_DRIFT = 0.015
const SOFT_SYNC_MAX_RATE_DELTA = 0.03
const PAIR_HARD_SEEK_THRESHOLD = isMobile ? 0.12 : 0.09
const VIDEO_LOOSE_SYNC_THRESHOLD = isMobile ? 0.35 : 0.25
const PREROLL_VOLUME = 0.001

// Toast
const showToast = ref(false)
const toastMessage = ref('')
let toastTimer = null

const trackMode = ref('original') 
const delay = ref(0) // Will be overwritten by loadSettings
const delayEnabled = ref(true)
const effectiveDelay = computed(() => (delayEnabled.value ? delay.value : 0))

// Detailed Volume Memory
const volOrigSolo = ref(100)
const volInstSolo = ref(100)
const volOrigMix = ref(80)
const volInstMix = ref(80)

let syncInterval = null
let sourceChangeToken = 0
let waitingDelayGate = false
let delayBootstrapTimer = null

const stopDelayBootstrapTimer = () => {
    if (delayBootstrapTimer !== null) {
        clearTimeout(delayBootstrapTimer)
        delayBootstrapTimer = null
    }
}

const applyPrerollVolumes = () => {
    if (origAudioRef.value) {
        origAudioRef.value.muted = false
        origAudioRef.value.volume = PREROLL_VOLUME
    }
    if (instAudioRef.value) {
        instAudioRef.value.muted = false
        instAudioRef.value.volume = PREROLL_VOLUME
    }
}

const startDelayBootstrap = () => {
    if (!videoRef.value) return

    stopDelayBootstrapTimer()

    // Delay disabled: never enter preroll timing logic.
    if (!delayEnabled.value) {
        waitingDelayGate = false
        immediateDelayCutover(true)
        return
    }

    waitingDelayGate = true
    applyPrerollVolumes()

    if (origAudioRef.value) {
        seekTo(origAudioRef.value, 0)
        origAudioRef.value.playbackRate = videoRef.value.playbackRate
        applyPlaybackState(origAudioRef.value)
    }
    if (instAudioRef.value) {
        seekTo(instAudioRef.value, 0)
        instAudioRef.value.playbackRate = videoRef.value.playbackRate
        applyPlaybackState(instAudioRef.value)
    }

    const timerMs = Math.max(100, Number(effectiveDelay.value) + 100)
    delayBootstrapTimer = setTimeout(() => {
        waitingDelayGate = false
        delayBootstrapTimer = null
        immediateDelayCutover(true)
        syncAudio()
    }, timerMs)
}

// Restore settings
const loadSettings = async () => {
    // 1. Try Server
    try {
        const res = await axios.get(`${API_BASE}/state`)
        const s = res.data
        if(s.trackMode) trackMode.value = s.trackMode
        if(s.delay) delay.value = Number(s.delay)
        if (s.delayEnabled !== undefined) delayEnabled.value = s.delayEnabled === '1' || s.delayEnabled === 'true'
        if(s.volOrigSolo) volOrigSolo.value = Number(s.volOrigSolo)
        if(s.volInstSolo) volInstSolo.value = Number(s.volInstSolo)
        if(s.volOrigMix) volOrigMix.value = Number(s.volOrigMix)
        if(s.volInstMix) volInstMix.value = Number(s.volInstMix)
        
        applyTrackMode()
        return // Success, skip local
    } catch(e) {
        console.log("State load failed, falling back to local", e)
    }

    // 2. Fallback Local
    const sDelay = localStorage.getItem('ktv_delay')
    if(sDelay !== null) delay.value = Number(sDelay)

    const sDelayEnabled = localStorage.getItem('ktv_delay_enabled')
    if (sDelayEnabled !== null) delayEnabled.value = sDelayEnabled === '1'

    const sVolOrigSolo = localStorage.getItem('ktv_vol_orig_solo')
    if(sVolOrigSolo !== null) volOrigSolo.value = Number(sVolOrigSolo)

    const sVolInstSolo = localStorage.getItem('ktv_vol_inst_solo')
    if(sVolInstSolo !== null) volInstSolo.value = Number(sVolInstSolo)

    const sVolOrigMix = localStorage.getItem('ktv_vol_orig_mix')
    if(sVolOrigMix !== null) volOrigMix.value = Number(sVolOrigMix)
    
    const sVolInstMix = localStorage.getItem('ktv_vol_inst_mix')
    if(sVolInstMix !== null) volInstMix.value = Number(sVolInstMix)
}

// Watch and Save
watch(delay, (n) => localStorage.setItem('ktv_delay', n))
watch(delayEnabled, (n) => localStorage.setItem('ktv_delay_enabled', n ? '1' : '0'))
watch(volOrigSolo, (n) => localStorage.setItem('ktv_vol_orig_solo', n))
watch(volInstSolo, (n) => localStorage.setItem('ktv_vol_inst_solo', n))
watch(volOrigMix, (n) => localStorage.setItem('ktv_vol_orig_mix', n))
watch(volInstMix, (n) => localStorage.setItem('ktv_vol_inst_mix', n))


const seekTo = (el, time) => {
    if (!el) return
    const safeTime = time < 0 ? 0 : time
    if (typeof el.fastSeek === 'function') el.fastSeek(safeTime)
    else el.currentTime = safeTime
}

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))

const applyPlaybackState = (audioEl) => {
    if (!audioEl || !videoRef.value) return
    if (videoRef.value.paused) {
        if (!audioEl.paused) audioEl.pause()
    } else if (audioEl.paused) {
        audioEl.play().catch(() => {})
    }
}

const applySoftRateSync = (audioEl, targetTime, baseRate = null) => {
    if (!audioEl || !videoRef.value) return 0

    const drift = targetTime - audioEl.currentTime
    const absDrift = Math.abs(drift)
    const refRate = baseRate ?? videoRef.value.playbackRate

    if (absDrift <= SOFT_SYNC_MIN_DRIFT || videoRef.value.paused) {
        if (Math.abs(audioEl.playbackRate - refRate) > 0.003) {
            audioEl.playbackRate = refRate
        }
        return absDrift
    }

    const desiredDelta = clamp(absDrift * 0.18, 0.004, SOFT_SYNC_MAX_RATE_DELTA)
    const nextRate = drift > 0 ? refRate + desiredDelta : refRate - desiredDelta
    audioEl.playbackRate = clamp(nextRate, 0.96, 1.04)
    return absDrift
}

const immediateDelayCutover = (restoreVolumes = true) => {
    if (!videoRef.value) return

    const rawTargetTime = videoRef.value.currentTime - (effectiveDelay.value / 1000)
    const targetTime = Math.max(0, rawTargetTime)
    const baseRate = videoRef.value.playbackRate

    if (origAudioRef.value) {
        seekTo(origAudioRef.value, targetTime)
        origAudioRef.value.playbackRate = baseRate
    }
    if (instAudioRef.value) {
        seekTo(instAudioRef.value, targetTime)
        instAudioRef.value.playbackRate = baseRate
    }

    waitingDelayGate = false

    applyPlaybackState(origAudioRef.value)
    applyPlaybackState(instAudioRef.value)
    if (restoreVolumes) applyTrackMode()
}

const syncAudio = () => {
    if (!videoRef.value) return
    if (needsGesture.value) {
        pauseAll()
        return
    }
    
    const vTime = videoRef.value.currentTime
    const targetTime = vTime - (effectiveDelay.value / 1000)

    if (waitingDelayGate) {
        applyPrerollVolumes()
        if (origAudioRef.value) {
            seekTo(origAudioRef.value, 0)
            origAudioRef.value.playbackRate = videoRef.value.playbackRate
            applyPlaybackState(origAudioRef.value)
        }
        if (instAudioRef.value) {
            seekTo(instAudioRef.value, 0)
            instAudioRef.value.playbackRate = videoRef.value.playbackRate
            applyPlaybackState(instAudioRef.value)
        }
        return
    }

    const safeTargetTime = Math.max(0, targetTime)
    const baseRate = videoRef.value.playbackRate

    applyPlaybackState(origAudioRef.value)
    applyPlaybackState(instAudioRef.value)

    // 1) 先做音轨间同步（优先目标）
    if (origAudioRef.value && instAudioRef.value) {
        const pairDrift = instAudioRef.value.currentTime - origAudioRef.value.currentTime
        const absPairDrift = Math.abs(pairDrift)

        if (absPairDrift > PAIR_HARD_SEEK_THRESHOLD) {
            // 仅对音轨间大偏差做一次切位，不暂停
            seekTo(instAudioRef.value, origAudioRef.value.currentTime)
            instAudioRef.value.playbackRate = baseRate
        } else {
            // 细同步：让伴奏贴近人声，速率变化尽量轻
            applySoftRateSync(instAudioRef.value, origAudioRef.value.currentTime, baseRate)
            if (Math.abs(origAudioRef.value.playbackRate - baseRate) > 0.003) {
                origAudioRef.value.playbackRate = baseRate
            }
        }
    } else {
        // 单轨兜底
        applySoftRateSync(origAudioRef.value, safeTargetTime, baseRate)
        applySoftRateSync(instAudioRef.value, safeTargetTime, baseRate)
    }

    // 2) 再做与视频的宽松约束（不追求严格对齐）
    if (origAudioRef.value && Math.abs(origAudioRef.value.currentTime - safeTargetTime) > VIDEO_LOOSE_SYNC_THRESHOLD) {
        seekTo(origAudioRef.value, safeTargetTime)
        origAudioRef.value.playbackRate = baseRate
    }
    if (instAudioRef.value && Math.abs(instAudioRef.value.currentTime - safeTargetTime) > VIDEO_LOOSE_SYNC_THRESHOLD) {
        seekTo(instAudioRef.value, safeTargetTime)
        instAudioRef.value.playbackRate = baseRate
    }
}

const waitForReady = (el) => new Promise((resolve) => {
    if (!el) return resolve()
    if (el.readyState >= 2) return resolve()
    const done = () => {
        el.removeEventListener('canplay', done)
        el.removeEventListener('loadedmetadata', done)
        el.removeEventListener('error', done)
        resolve()
    }
    el.addEventListener('canplay', done, { once: true })
    el.addEventListener('loadedmetadata', done, { once: true })
    el.addEventListener('error', done, { once: true })
    setTimeout(done, 2000)
})

const syncAfterSourceChange = async () => {
    const token = ++sourceChangeToken
    pauseAll()
    if (videoRef.value) {
        videoRef.value.currentTime = 0
        videoRef.value.load()
    }
    if (origAudioRef.value) {
        origAudioRef.value.currentTime = 0
        origAudioRef.value.load()
    }
    if (instAudioRef.value) {
        instAudioRef.value.currentTime = 0
        instAudioRef.value.load()
    }

    await Promise.all([
        waitForReady(videoRef.value),
        waitForReady(origAudioRef.value),
        waitForReady(instAudioRef.value)
    ])

    if (token !== sourceChangeToken) return
    syncAudio()
    if (!needsGesture.value) {
        await tryPlay(videoRef.value, true)
        if (delayEnabled.value) startDelayBootstrap()
        else syncAudio()
    }
}

const updateSync = () => { syncAudio() }
const onDelayInput = () => { immediateDelayCutover() }

const updateVolume = () => { applyTrackMode() }

const isAutoplayBlocked = (err) => {
    const name = err?.name || ''
    return name === 'NotAllowedError' || name === 'NotSupportedError' || /NotAllowedError/i.test(String(err))
}

const tryPlay = async (el, requireGesture = false) => {
    if (!el) return true
    try {
        await el.play()
        return true
    } catch (err) {
        if (requireGesture && isAutoplayBlocked(err)) {
            needsGesture.value = true
            pauseAll()
        }
        return false
    }
}

const pauseAll = () => {
    stopDelayBootstrapTimer()
    if (videoRef.value && !videoRef.value.paused) videoRef.value.pause()
    if (origAudioRef.value && !origAudioRef.value.paused) origAudioRef.value.pause()
    if (instAudioRef.value && !instAudioRef.value.paused) instAudioRef.value.pause()
}

const unlockAudio = async () => {
    needsGesture.value = false
    await tryPlay(videoRef.value, true)
    syncAudio()
}

const toggleDelay = () => {
    delayEnabled.value = !delayEnabled.value
    waitingDelayGate = false
    stopDelayBootstrapTimer()
    immediateDelayCutover(true)
}

const setTrack = (mode) => {
    trackMode.value = mode
    applyTrackMode()
    
    // Show Toast
    if (mode === 'original') toastMessage.value = 'Original Vocals'
    else if (mode === 'instrumental') toastMessage.value = 'Instrumental'
    else toastMessage.value = 'Mix Mode'
    
    showToast.value = true
    if(toastTimer) clearTimeout(toastTimer)
    toastTimer = setTimeout(() => { showToast.value = false }, 3000)
}

const restartPlayback = async () => {
    if (!videoRef.value) return
    waitingDelayGate = false
    stopDelayBootstrapTimer()

    videoRef.value.currentTime = 0
    if (origAudioRef.value) seekTo(origAudioRef.value, 0)
    if (instAudioRef.value) seekTo(instAudioRef.value, 0)

    immediateDelayCutover()

    if (!needsGesture.value) {
        await tryPlay(videoRef.value, true)
        startDelayBootstrap()
    }
}

const applyTrackMode = () => {
    if (!videoRef.value) return

    if (waitingDelayGate) {
        applyPrerollVolumes()
        return
    }
    
    // Video always Muted
    videoRef.value.muted = true
    
    if (trackMode.value === 'original') {
        if(origAudioRef.value) {
            origAudioRef.value.muted = false
            origAudioRef.value.volume = volOrigSolo.value / 100
        }
        if(instAudioRef.value) {
               // Keep active but silent (0.1%) to prevent browser deprioritization/desync
               instAudioRef.value.muted = false
             instAudioRef.value.volume = PREROLL_VOLUME
        }
    } 
    else if (trackMode.value === 'instrumental') {
        if(origAudioRef.value) {
               // Keep active but silent (0.1%) to prevent browser deprioritization/desync
               origAudioRef.value.muted = false
             origAudioRef.value.volume = PREROLL_VOLUME
        }
        if(instAudioRef.value) {
            instAudioRef.value.muted = false
             instAudioRef.value.volume = volInstSolo.value / 100
        }
    } 
    else if (trackMode.value === 'mix') {
        if(origAudioRef.value) {
            origAudioRef.value.muted = false
            origAudioRef.value.volume = volOrigMix.value / 100
        }
        if(instAudioRef.value) {
            instAudioRef.value.muted = false
             instAudioRef.value.volume = volInstMix.value / 100
        }
    }
}

onMounted(() => {
    loadSettings()

    player.value = new Plyr(videoRef.value, {
        controls: ['play-large', 'play', 'progress', 'current-time', 'fullscreen'], 
        keyboard: { focused: true, global: true },
        muted: true,
        autoplay: true  // Enable AutoPlay
    })
    
    // Explicitly try to play after ready
    player.value.on('ready', () => {
        player.value.play().catch((err) => {
            if (isAutoplayBlocked(err)) {
                needsGesture.value = true
                pauseAll()
            }
        })
    })
    
    const v = videoRef.value
    // Ensure all start together
    const playAll = () => {
        if (needsGesture.value) {
            pauseAll()
            return
        }
            if (videoRef.value && videoRef.value.currentTime < 0.35 && delayEnabled.value) {
                startDelayBootstrap()
            } else {
                syncAudio()
            }
        tryPlay(origAudioRef.value)
        tryPlay(instAudioRef.value)
    }
    if(v) {
        v.addEventListener('play', playAll)
        v.addEventListener('playing', playAll)
        v.addEventListener('pause', pauseAll)
        v.addEventListener('waiting', pauseAll)
        v.addEventListener('seeking', syncAudio)
        v.addEventListener('seeked', syncAudio)
    }

    syncInterval = setInterval(syncAudio, SYNC_CHECK_INTERVAL_MS)
    
    // Resume Track Mode
    const savedMode = localStorage.getItem('ktv_track_mode')
    if(savedMode) trackMode.value = savedMode
    
    applyTrackMode()

    const onUserGesture = () => {
        if (needsGesture.value) unlockAudio()
    }
    window.addEventListener('pointerdown', onUserGesture, { passive: true })
    window.addEventListener('touchstart', onUserGesture, { passive: true })
    window.addEventListener('keydown', onUserGesture)

    onUnmounted(() => {
        window.removeEventListener('pointerdown', onUserGesture)
        window.removeEventListener('touchstart', onUserGesture)
        window.removeEventListener('keydown', onUserGesture)
    })
})

watch(
    () => [props.videoSrc, props.audioSrc, props.originalAudioSrc],
    () => {
        syncAfterSourceChange()
    }
)

onUnmounted(() => {
    stopDelayBootstrapTimer()
    if (syncInterval) clearInterval(syncInterval)
    if (player.value) player.value.destroy()
})

defineExpose({
    setTrack: (mode) => { 
        trackMode.value = mode
        localStorage.setItem('ktv_track_mode', mode)
        setTrack(mode) 
    },
    updateSync, // effectively forces update
    setVolume: (target, val) => {
        if(target === 'volOrigSolo') volOrigSolo.value = Number(val)
        if(target === 'volInstSolo') volInstSolo.value = Number(val)
        if(target === 'volOrigMix') volOrigMix.value = Number(val)
        if(target === 'volInstMix') volInstMix.value = Number(val)
        applyTrackMode()
    },
    togglePlay: () => {
        if(player.value) player.value.togglePlay()
    },
    restart: () => {
        restartPlayback()
    },
    setDelayEnabled: (val) => {
        delayEnabled.value = !!val
        waitingDelayGate = false
        stopDelayBootstrapTimer()
        immediateDelayCutover()
    },
    setDelay: (val) => {
        delay.value = Number(val)
        waitingDelayGate = false
        stopDelayBootstrapTimer()
        immediateDelayCutover()
    },
    delay, // ref exposed
    delayEnabled
})
</script>

<style scoped>
.ktv-player {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    position: relative;
}

.track-toast {
    position: absolute;
    top: 20px;
    left: 20px;
    background: rgba(0, 0, 0, 0.7);
    color: white;
    padding: 10px 20px;
    border-radius: 8px;
    z-index: 100;
    font-size: 1.2em;
    pointer-events: none;
    border: 1px solid rgba(255, 255, 255, 0.2);
    backdrop-filter: blur(4px);
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
}

.gesture-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.55);
    color: #fff;
    font-size: 1.2em;
    z-index: 120;
    user-select: none;
}

.fade-enter-active, .fade-leave-active {
    transition: opacity 0.5s ease;
}
.fade-enter-from, .fade-leave-to {
    opacity: 0;
}

.main-video {
    width: 100%;
    height: 100%;
    object-fit: contain;
}
/* Force Plyr to fill */
:deep(.plyr) {
    width: 100%;
    height: 100%;
}
:deep(.plyr__video-wrapper) {
    width: 100%;
    height: 100%;
}
:deep(video) {
    object-fit: contain !important;
}
.controls-ui {
    margin-top: 15px;
    background: #333;
    padding: 15px;
    border-radius: 8px;
}
.control-group {
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
}
button.active {
    background-color: #646cff; /* Vue Greenish/Blue */
    color: white;
}
</style>