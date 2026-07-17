<template>
  <div class="player-view">
    <div v-if="currentSong && !isMusicMode" class="video-container">
      <KTVPlayer
        ref="playerRef"
        :videoSrc="getMediaUrl(currentSong.video_path)"
        :audioSrc="getMediaUrl(currentSong.audio_path)"
        :originalAudioSrc="getMediaUrl(currentSong.raw_audio_path || currentSong.video_path)"
        :showControls="false"
        @ended="onSongEnded"
      />
      <div v-if="showSongOverlay" class="song-overlay"><h2>{{ currentSong.title }}</h2></div>
    </div>

    <div v-else-if="currentSong && isMusicMode" class="music-mode">
      <audio
        ref="origMusicRef"
        crossorigin
        :src="getMediaUrl(currentSong.raw_audio_path || currentSong.audio_path)"
        @ended="onSongEnded"
        @timeupdate="onMusicTimeUpdate"
      ></audio>
      <audio ref="instMusicRef" crossorigin :src="getMediaUrl(currentSong.audio_path)"></audio>
      <div v-if="musicNeedsGesture" class="music-gesture-overlay" @click="unlockMusicAudio">
        点击屏幕启用声音
      </div>

      <div class="cover-panel">
        <img v-if="musicCoverUrl" :src="musicCoverUrl" class="cover-img" alt="cover" />
        <div v-else class="cover-fallback"></div>
        <div class="music-title">{{ currentSong.title || 'Unknown' }}</div>
        <div class="music-singer">{{ currentSong.singers || 'Unknown' }}</div>
      </div>

      <div class="lyric-panel" ref="lyricPanelRef">
        <div v-if="lyricLines.length === 0" class="no-lyric">无歌词</div>
        <div v-else>
          <div
            v-for="(line, idx) in lyricLines"
            :key="`${idx}-${line.time}`"
            :class="['lyric-line', { active: idx === activeLyricIndex }]"
          >
            {{ line.text || '...' }}
          </div>
        </div>
      </div>
    </div>

    <div v-else class="idle-state">
      <div v-if="nextSong" class="next-song-info">
        <h2>Next Up: {{ nextSong.title || nextSong.url }}</h2>
        <div class="status-display">
          <div class="spinner" v-if="nextSong.status !== 'completed'"></div>
          <div class="status-text">
            <h3>{{ nextSong.status.toUpperCase() }}</h3>
            <p>{{ nextSong.status_detail }}</p>
            <div class="progress-bar" v-if="nextSong.status !== 'completed' && nextSong.status !== 'pending'">
              <div class="fill" :style="{ width: nextSong.progress + '%' }"></div>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="empty-queue">
        <h1>🎵 KTV System</h1>
        <p>No songs in queue. Please add songs from the Remote.</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed, watch, nextTick } from 'vue'
import axios from 'axios'
import KTVPlayer from '../components/KTVPlayer.vue'
import { AudioEngine } from '../audio/AudioEngine.js'
import { hardAlignTracks, syncFollowerToMaster } from '../audio/audioSync.js'
import { API_BASE, getWebSocketUrl } from '../network.js'

const WS_BASE = getWebSocketUrl('/ws/player')

const currentSong = ref(null)
const songs = ref([])
const playerRef = ref(null)

const origMusicRef = ref(null)
const instMusicRef = ref(null)
const lyricPanelRef = ref(null)
const lyricLines = ref([])
const activeLyricIndex = ref(-1)
const musicCoverUrl = ref('')
const showSongOverlay = ref(false)
const musicNeedsGesture = ref(false)

const trackMode = ref('original')
const volOrigSolo = ref(100)
const volInstSolo = ref(100)
const volOrigMix = ref(80)
const volInstMix = ref(80)
const pitchSemitones = ref(0)
const pitchEnabled = ref(false)

let ws = null
let refreshInterval = null
let musicSyncTimer = null
let songOverlayTimer = null
let musicAudioEngine = null
let queueActionInFlight = false

const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
const MUSIC_SYNC_INTERVAL_MS = isMobile ? 100 : 70

const nextSong = computed(() => {
  const actionable = songs.value.find((song) => (song.status || '').toLowerCase() !== 'pending')
  return actionable || songs.value[0] || null
})
const isMusicMode = computed(() => (currentSong.value?.media_type || 'video') === 'music')
const ERROR_STATUSES = new Set(['error', 'failed'])

const getMediaUrl = (url) => {
  if (!url) return ''
  if (url.startsWith('http')) return url
  const normalized = String(url).replace(/\\/g, '/')
  const rel = normalized.startsWith('/songs') || normalized.startsWith('songs') ? normalized : `/songs/${normalized}`
  const path = rel.startsWith('/') ? rel : `/${rel}`
  return `${API_BASE}${path}`
}

const applyMusicTrackMode = () => {
  const orig = origMusicRef.value
  const inst = instMusicRef.value
  if (!orig || !inst) return

  const silent = 0.001
  let originalGain = silent
  let instrumentalGain = silent
  if (trackMode.value === 'original') {
    originalGain = volOrigSolo.value / 100
  } else if (trackMode.value === 'instrumental') {
    instrumentalGain = volInstSolo.value / 100
  } else {
    originalGain = volOrigMix.value / 100
    instrumentalGain = volInstMix.value / 100
  }

  if (musicAudioEngine) {
    musicAudioEngine.setTrackGains(originalGain, instrumentalGain)
  } else {
    orig.volume = originalGain
    inst.volume = instrumentalGain
  }
}

const applyMusicPitch = () => {
  musicAudioEngine?.setPitch(pitchSemitones.value, pitchEnabled.value)
}

const syncMusicTracks = () => {
  const orig = origMusicRef.value
  const inst = instMusicRef.value
  if (!orig || !inst) return

  if (orig.paused) {
    if (!inst.paused) inst.pause()
    return
  }
  if (inst.paused) inst.play().catch(() => {})

  const baseRate = 1
  orig.playbackRate = baseRate
  syncFollowerToMaster(orig, inst, baseRate)
}

const waitAudioReady = (audioEl, timeoutMs = 1500) => new Promise((resolve) => {
  if (!audioEl) {
    resolve()
    return
  }
  if (audioEl.readyState >= 2) {
    resolve()
    return
  }
  let done = false
  const onDone = () => {
    if (done) return
    done = true
    audioEl.removeEventListener('canplay', onDone)
    audioEl.removeEventListener('loadedmetadata', onDone)
    resolve()
  }
  audioEl.addEventListener('canplay', onDone, { once: true })
  audioEl.addEventListener('loadedmetadata', onDone, { once: true })
  setTimeout(onDone, timeoutMs)
})

const parseLrc = (rawText) => {
  const lines = String(rawText || '').split(/\r?\n/)
  const parsed = []
  const reg = /\[(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?\]/g
  for (const rawLine of lines) {
    const text = rawLine.replace(reg, '').trim()
    reg.lastIndex = 0
    let m = reg.exec(rawLine)
    while (m) {
      const min = Number(m[1] || 0)
      const sec = Number(m[2] || 0)
      const ms = Number((m[3] || '0').padEnd(3, '0'))
      parsed.push({ time: min * 60 + sec + ms / 1000, text })
      m = reg.exec(rawLine)
    }
  }
  parsed.sort((a, b) => a.time - b.time)
  return parsed
}

const loadMusicLyric = async (song) => {
  lyricLines.value = []
  activeLyricIndex.value = -1
  const lyricUrl = getMediaUrl(song?.lyric_path)
  if (!lyricUrl) return
  try {
    const res = await axios.get(lyricUrl)
    lyricLines.value = parseLrc(res.data)
  } catch (e) {
    lyricLines.value = []
  }
}

const startMusicPlayback = async () => {
  const orig = origMusicRef.value
  const inst = instMusicRef.value
  if (!orig || !inst) return

  if (musicSyncTimer) {
    clearInterval(musicSyncTimer)
    musicSyncTimer = null
  }

  await Promise.all([waitAudioReady(orig), waitAudioReady(inst)])

  if (!musicAudioEngine) {
    try {
      musicAudioEngine = new AudioEngine(orig, inst)
      await musicAudioEngine.init()
      applyMusicPitch()
    } catch (e) {
      console.error('High-quality music pitch shift unavailable; using original media output', e)
      musicAudioEngine = null
    }
  }

  hardAlignTracks(orig, inst, 0, 1)
  applyMusicTrackMode()

  try {
    const running = await musicAudioEngine?.resume()
    if (musicAudioEngine && !running) {
      musicNeedsGesture.value = true
      return
    }
  } catch (e) {
    musicNeedsGesture.value = true
    return
  }

  musicNeedsGesture.value = false
  const playResults = await Promise.allSettled([orig.play(), inst.play()])
  if (playResults.some((result) => result.status === 'rejected')) {
    orig.pause()
    inst.pause()
    musicNeedsGesture.value = true
    return
  }
  if (Math.abs(inst.currentTime - orig.currentTime) > 0.01) {
    inst.currentTime = orig.currentTime
  }

  syncMusicTracks()
  musicSyncTimer = setInterval(syncMusicTracks, MUSIC_SYNC_INTERVAL_MS)
}

const unlockMusicAudio = async () => {
  try {
    await musicAudioEngine?.resume()
    musicNeedsGesture.value = false
    await startMusicPlayback()
  } catch (e) {
    musicNeedsGesture.value = true
  }
}

const stopMusicPlayback = () => {
  if (musicSyncTimer) {
    clearInterval(musicSyncTimer)
    musicSyncTimer = null
  }
  if (origMusicRef.value && !origMusicRef.value.paused) origMusicRef.value.pause()
  if (instMusicRef.value && !instMusicRef.value.paused) instMusicRef.value.pause()
  if (musicAudioEngine) musicAudioEngine.dispose()
  musicAudioEngine = null
  musicNeedsGesture.value = false
}

const fetchSongs = async () => {
  try {
    const res = await axios.get(`${API_BASE}/songs`)
    songs.value = res.data
    checkAutoPlay()
  } catch (e) {}
}

const checkAutoPlay = () => {
  if (!currentSong.value && nextSong.value) {
    const next = nextSong.value
    if (ERROR_STATUSES.has((next.status || '').toLowerCase())) {
      skipQueueHead(next)
      return
    }
    if (next.status === 'completed') {
      playSong(next)
    }
  }
}

const skipQueueHead = async (song) => {
  if (!song || queueActionInFlight) return
  queueActionInFlight = true
  try {
    await axios.delete(`${API_BASE}/song/${song.id}`)
  } catch (e) {}
  currentSong.value = null
  try {
    await fetchSongs()
  } finally {
    queueActionInFlight = false
  }
}

const triggerSongOverlay = () => {
  showSongOverlay.value = true
  if (songOverlayTimer) clearTimeout(songOverlayTimer)
  songOverlayTimer = setTimeout(() => {
    showSongOverlay.value = false
    songOverlayTimer = null
  }, 4000)
}

const playSong = async (song) => {
  currentSong.value = song
  triggerSongOverlay()
  musicCoverUrl.value = getMediaUrl(song.cover_path)
  if ((song.media_type || 'video') === 'music') {
    await loadMusicLyric(song)
    await nextTick()
    await startMusicPlayback()
  }
}

const onSongEnded = async () => {
  if (currentSong.value) {
    stopMusicPlayback()
    showSongOverlay.value = false
    if (songOverlayTimer) {
      clearTimeout(songOverlayTimer)
      songOverlayTimer = null
    }
    try {
      await axios.delete(`${API_BASE}/song/${currentSong.value.id}`)
    } catch (e) {}
    currentSong.value = null
    lyricLines.value = []
    activeLyricIndex.value = -1
    fetchSongs()
  }
}

const skipCurrentOrHead = async () => {
  if (currentSong.value) {
    await onSongEnded()
    return
  }
  if (nextSong.value) {
    await skipQueueHead(nextSong.value)
  }
}

const onMusicTimeUpdate = () => {
  const orig = origMusicRef.value
  if (!orig || lyricLines.value.length === 0) return
  const t = orig.currentTime
  let idx = -1
  for (let i = 0; i < lyricLines.value.length; i++) {
    if (lyricLines.value[i].time <= t) idx = i
    else break
  }
  activeLyricIndex.value = idx
}

watch(activeLyricIndex, async (idx) => {
  if (idx < 0) return
  await nextTick()
  const panel = lyricPanelRef.value
  if (!panel) return
  const active = panel.querySelector('.lyric-line.active')
  if (!active) return
  const top = active.offsetTop - panel.clientHeight / 2 + active.clientHeight / 2
  panel.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
})

watch([trackMode, volOrigSolo, volInstSolo, volOrigMix, volInstMix], () => {
  if (isMusicMode.value) applyMusicTrackMode()
})

watch([pitchSemitones, pitchEnabled], () => {
  if (isMusicMode.value) applyMusicPitch()
})

const refreshPitchState = async () => {
  try {
    const res = await axios.get(`${API_BASE}/state`)
    const s = res.data || {}
    if (s.pitchSemitones !== undefined) pitchSemitones.value = Number(s.pitchSemitones)
    if (s.pitchEnabled !== undefined) pitchEnabled.value = s.pitchEnabled === '1' || s.pitchEnabled === 'true'
    if (isMusicMode.value) {
      applyMusicPitch()
    } else if (playerRef.value) {
      playerRef.value.setPitch(pitchSemitones.value)
      playerRef.value.setPitchEnabled(pitchEnabled.value)
    }
  } catch (e) {}
}

const connectWS = () => {
  ws = new WebSocket(WS_BASE)
  ws.onopen = () => refreshPitchState()
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    handleRemoteCommand(data)
  }
  ws.onclose = () => setTimeout(connectWS, 3000)
}

const handleRemoteCommand = (cmd) => {
  if (isMusicMode.value) {
    const orig = origMusicRef.value
    const inst = instMusicRef.value
    if (!orig || !inst) return

    switch (cmd.action) {
      case 'setTrack':
        trackMode.value = cmd.value || 'original'
        applyMusicTrackMode()
        break
      case 'setVolume': {
        const val = Number(cmd.value)
        if (cmd.target === 'volOrigSolo') volOrigSolo.value = val
        if (cmd.target === 'volInstSolo') volInstSolo.value = val
        if (cmd.target === 'volOrigMix') volOrigMix.value = val
        if (cmd.target === 'volInstMix') volInstMix.value = val
        applyMusicTrackMode()
        break
      }
      case 'setDelay':
      case 'setDelayEnabled':
        break
      case 'setPitch':
        pitchSemitones.value = Number(cmd.value) || 0
        applyMusicPitch()
        syncMusicTracks()
        break
      case 'setPitchEnabled':
        pitchEnabled.value = !!cmd.value
        applyMusicPitch()
        syncMusicTracks()
        break
      case 'skip':
        skipCurrentOrHead()
        break
      case 'pause':
        if (orig.paused) {
          orig.play().catch(() => {})
          inst.play().catch(() => {})
        } else {
          orig.pause()
          inst.pause()
        }
        break
      case 'restart':
        orig.currentTime = 0
        inst.currentTime = 0
        orig.play().catch(() => {})
        inst.play().catch(() => {})
        break
      default:
        break
    }
    return
  }

  if (!playerRef.value) return
  switch (cmd.action) {
    case 'setTrack':
      trackMode.value = cmd.value || 'original'
      playerRef.value.setTrack(cmd.value)
      break
    case 'setDelay':
      if (playerRef.value.setDelay) playerRef.value.setDelay(cmd.value)
      break
    case 'setDelayEnabled':
      if (playerRef.value.setDelayEnabled) playerRef.value.setDelayEnabled(!!cmd.value)
      break
    case 'setVolume':
      playerRef.value.setVolume(cmd.target, cmd.value)
      break
    case 'setPitch':
      pitchSemitones.value = Number(cmd.value) || 0
      playerRef.value.setPitch(cmd.value)
      break
    case 'setPitchEnabled':
      pitchEnabled.value = !!cmd.value
      playerRef.value.setPitchEnabled(!!cmd.value)
      break
    case 'skip':
      skipCurrentOrHead()
      break
    case 'pause':
      playerRef.value.togglePlay()
      break
    case 'restart':
      playerRef.value.restart()
      break
    default:
      break
  }
}

onMounted(async () => {
  try {
    const res = await axios.get(`${API_BASE}/state`)
    const s = res.data
    if (s.trackMode) trackMode.value = s.trackMode
    if (s.volOrigSolo) volOrigSolo.value = Number(s.volOrigSolo)
    if (s.volInstSolo) volInstSolo.value = Number(s.volInstSolo)
    if (s.volOrigMix) volOrigMix.value = Number(s.volOrigMix)
    if (s.volInstMix) volInstMix.value = Number(s.volInstMix)
    if (s.pitchSemitones !== undefined) pitchSemitones.value = Number(s.pitchSemitones)
    if (s.pitchEnabled !== undefined) pitchEnabled.value = s.pitchEnabled === '1' || s.pitchEnabled === 'true'
  } catch (e) {}

  fetchSongs()
  refreshInterval = setInterval(fetchSongs, 2000)
  connectWS()
})

onUnmounted(() => {
  if (refreshInterval) clearInterval(refreshInterval)
  if (ws) ws.close()
  stopMusicPlayback()
  if (songOverlayTimer) clearTimeout(songOverlayTimer)
})
</script>

<style scoped>
.player-view {
  background: #000;
  width: 100vw;
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  overflow: hidden;
}
.video-container {
  width: 100%;
  height: 100%;
}
.song-overlay {
  position: absolute;
  left: 20px;
  bottom: 20px;
  background: rgba(0, 0, 0, 0.45);
  border-radius: 8px;
  padding: 8px 14px;
}
.music-mode {
  position: relative;
  width: 100%;
  height: 100%;
  display: grid;
  grid-template-columns: 40% 60%;
}
.music-gesture-overlay {
  position: absolute;
  inset: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  font-size: 24px;
  cursor: pointer;
}
.cover-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 20px;
}
.cover-img,
.cover-fallback {
  width: 70%;
  aspect-ratio: 1 / 1;
  border-radius: 12px;
  object-fit: cover;
}
.cover-fallback {
  background: #000;
  border: 1px solid #1f1f1f;
}
.music-title {
  font-size: 28px;
  font-weight: 700;
  text-align: center;
}
.music-singer {
  color: #bcbcbc;
  text-align: center;
}
.lyric-panel {
  height: 100%;
  overflow-y: auto;
  padding: 40px 34px;
  box-sizing: border-box;
}
.lyric-line {
  color: #8f8f8f;
  font-size: 36px;
  line-height: 1.5;
  transition: color 0.2s, transform 0.2s, font-size 0.2s;
}
.lyric-line.active {
  color: #ffffff;
  font-size: 39.6px;
  transform: scale(1.02);
}
.no-lyric {
  color: #969696;
  font-size: 36px;
  margin-top: 20%;
}
.idle-state { text-align: center; }
.spinner {
  width: 50px;
  height: 50px;
  border: 5px solid #333;
  border-top: 5px solid #1a73e8;
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin: 20px auto;
}
.progress-bar {
  width: 300px;
  height: 10px;
  background: #333;
  border-radius: 5px;
  margin: 10px auto;
  overflow: hidden;
}
.fill {
  height: 100%;
  background: #1a73e8;
  transition: width 0.3s;
}
@keyframes spin { 0% { transform: rotate(0deg);} 100% { transform: rotate(360deg);} }
</style>
