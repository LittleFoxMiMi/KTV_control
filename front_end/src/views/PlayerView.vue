<template>
  <div class="player-view">
    <!-- Active Player -->
    <div v-if="currentSong" class="video-container">
       <KTVPlayer 
          ref="playerRef"
          :videoSrc="getMediaUrl(currentSong.video_path)" 
          :audioSrc="getMediaUrl(currentSong.audio_path)"
          :originalAudioSrc="getMediaUrl(currentSong.raw_audio_path || currentSong.video_path)"
          :showControls="false"
          @ended="onSongEnded"
          @timeupdate="onTimeUpdate"
       />
       <div class="song-overlay">
         <h2>{{ currentSong.title }}</h2>
       </div>
    </div>

    <!-- Idle / Next Song Status -->
    <div v-else class="idle-state">
       <div v-if="nextSong" class="next-song-info">
          <h2>Next Up: {{ nextSong.title || nextSong.url }}</h2>
          <div class="status-display">
             <div class="spinner" v-if="nextSong.status !== 'completed'"></div>
             <div class="status-text">
                 <h3>{{ nextSong.status.toUpperCase() }}</h3>
                 <p>{{ nextSong.status_detail }}</p>
                 <div class="progress-bar" v-if="nextSong.status !== 'completed' && nextSong.status !== 'pending'">
                    <div class="fill" :style="{width: nextSong.progress + '%'}"></div>
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
import { ref, onMounted, onUnmounted, computed } from 'vue'
import axios from 'axios'
import KTVPlayer from '../components/KTVPlayer.vue'

// Dynamic Host Detection
const getHost = () => window.location.hostname
const API_BASE = `http://${getHost()}:8000`
const WS_BASE = `ws://${getHost()}:8000/ws/player`

const currentSong = ref(null)
const songs = ref([])
const playerRef = ref(null)
let ws = null
let refreshInterval = null

// Helper
const getMediaUrl = (url) => {
    if (!url) return ''
    if (url.startsWith('http')) return url
    
    // Normalize slashes
    url = url.replace(/\\/g, '/')
    
    // Ensure relative to songs dir
    if (!url.startsWith('/songs') && !url.startsWith('songs')) {
        url = '/songs/' + url
    }
    
    // Ensure leading slash
    if (!url.startsWith('/')) url = '/' + url
    
    return `${API_BASE}${url}`
}

const nextSong = computed(() => {
    return songs.value.length > 0 ? songs.value[0] : null
})

const ERROR_STATUSES = new Set(['error', 'failed'])

// Queue Management
const fetchSongs = async () => {
    try {
        const res = await axios.get(`${API_BASE}/songs`)
        songs.value = res.data
        checkAutoPlay()
    } catch (e) {}
}

const checkAutoPlay = () => {
    // Basic Check: If no song playing, try to play the first one
    if (!currentSong.value && songs.value.length > 0) {
        const next = songs.value[0]

        // If next song errored, drop it and move on
        if (ERROR_STATUSES.has((next.status || '').toLowerCase())) {
            skipQueueHead(next)
            return
        }
        
        // If next song is ready, play it
        if (next.status === 'completed') {
            playSong(next)
        } 
        else if (next.status === 'processing' || next.status === 'downloading') {
            // It's not ready yet, just wait. The polling will catch it when it turns 'completed'.
            console.log("Next song is preparing...", next.title)
        }
    }
}

const skipQueueHead = async (song) => {
    if (!song) return
    try {
        await axios.delete(`${API_BASE}/song/${song.id}`)
    } catch (e) {}
    currentSong.value = null
    fetchSongs()
}

const playSong = async (song) => {
    currentSong.value = song
    // Remove from queue locally? No, usually delete from server when finished.
    // We'll delete from server when song ends.
}

const onSongEnded = async () => {
    if (currentSong.value) {
       // Delete from DB and move next
       try {
           await axios.delete(`${API_BASE}/song/${currentSong.value.id}`)
       } catch(e) {}
       currentSong.value = null
       fetchSongs() // This will trigger checkAutoPlay -> Play next
    }
}

const skipCurrentOrHead = async () => {
    if (currentSong.value) {
        await onSongEnded()
        return
    }
    if (songs.value.length > 0) {
        await skipQueueHead(songs.value[0])
    }
}

const onTimeUpdate = (time) => {
   // Send time to remote (throttled)
   if(ws && ws.readyState === WebSocket.OPEN) {
       // Ideally throttle this
       // ws.send(JSON.stringify({ type: 'status', time: time })) 
   }
}

// WebSocket Logic
const connectWS = () => {
    ws = new WebSocket(WS_BASE)
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data)
        handleRemoteCommand(data)
    }
    ws.onclose = () => {
        setTimeout(connectWS, 3000)
    }
}

const handleRemoteCommand = (cmd) => {
    if (!playerRef.value) return

    switch(cmd.action) {
        case 'setTrack':
            playerRef.value.setTrack(cmd.value)
            break
        case 'setDelay':
             if (playerRef.value.setDelay) playerRef.value.setDelay(cmd.value)
             else {
                 playerRef.value.delay = cmd.value
                 playerRef.value.updateSync()
             }
            break
           case 'setDelayEnabled':
               if (playerRef.value.setDelayEnabled) playerRef.value.setDelayEnabled(!!cmd.value)
               break
        case 'setVolume':
             // cmd.target = 'origSolo' | 'instMix' etc.
             playerRef.value.setVolume(cmd.target, cmd.value)
             break
        case 'skip':
               skipCurrentOrHead()
             break
        case 'pause':
             // Need to expose pause in KTVPlayer
             playerRef.value.togglePlay()
             break
        case 'restart':
             playerRef.value.restart()
             break
    }
}

onMounted(() => {
    fetchSongs()
    refreshInterval = setInterval(fetchSongs, 2000)
    connectWS()
})

onUnmounted(() => {
    if (refreshInterval) clearInterval(refreshInterval)
    if (ws) ws.close()
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
.idle-state {
    text-align: center;
}
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
@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
</style>
