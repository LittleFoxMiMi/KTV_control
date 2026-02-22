<template>
  <div class="remote-view">
    <div class="header">
        <h1>📱 KTV Remote</h1>
        <div class="connection-status" :class="{connected: isConnected}"></div>
    </div>
    
    <!-- Add Song -->
    <div class="card add-box">
        <input v-model="newUrl" placeholder="Bilibili / YouTube URL" @keyup.enter="addSong" />
        <button @click="addSong" :disabled="loading">Add</button>
    </div>
    <p class="msg">{{ message }}</p>

    <!-- Controls (No Wrapper / Direct Layout) -->
    
    <!-- Row 1: Play & Skip -->
    <div class="control-row-direct">
        <button class="btn-warn" v-on:click="sendAction('skip')">⏭ Skip</button>
        <button class="btn-main" v-on:click="sendAction('pause')">⏯ Play/Pause</button>
        <button class="btn-main" v-on:click="sendAction('restart')">↺ Restart</button>
    </div>
    
    <!-- Row 2: Mode Select -->
    <div class="control-row-direct mode-select">
        <button @click="setTrack('original')" :class="{active: trackMode==='original'}">Vocals</button>
        <button @click="setTrack('instrumental')" :class="{active: trackMode==='instrumental'}">Instr.</button>
        <button @click="setTrack('mix')" :class="{active: trackMode==='mix'}">Mix</button>
        <button @click="toggleDelay" :class="{active: delayEnabled}">Delay</button>
    </div>

    <!-- Volumes / Delay in Card -->
    <div class="card settings-box">
        <div class="control-group">
            <label>Delay: {{ delayEnabled ? delay : 0 }}ms</label>
            <div class="stepper">
                <button @click="updateDelay(delay - 50)" class="step-btn" :disabled="!delayEnabled">-</button>
                <div class="step-val">{{ delay }}</div>
                <button @click="updateDelay(delay + 50)" class="step-btn" :disabled="!delayEnabled">+</button>
            </div>
        </div>

        <!-- Volume Controls (Buttons) -->
        <div v-if="trackMode==='original'" class="control-group">
             <label>Vocals Vol</label>
             <div class="stepper">
                <button @click="updateVol('volOrigSolo', volOrigSolo - 5)" class="step-btn">-</button>
                <div class="step-val">{{ volOrigSolo }}</div>
                <button @click="updateVol('volOrigSolo', volOrigSolo + 5)" class="step-btn">+</button>
             </div>
        </div>
        <div v-if="trackMode==='instrumental'" class="control-group">
             <label>Inst Vol</label>
             <div class="stepper">
                <button @click="updateVol('volInstSolo', volInstSolo - 5)" class="step-btn">-</button>
                <div class="step-val">{{ volInstSolo }}</div>
                <button @click="updateVol('volInstSolo', volInstSolo + 5)" class="step-btn">+</button>
             </div>
        </div>
        <div v-if="trackMode==='mix'" class="control-group">
             <label>Vocals</label>
             <div class="stepper">
                <button @click="updateVol('volOrigMix', volOrigMix - 5)" class="step-btn">-</button>
                <div class="step-val">{{ volOrigMix }}</div>
                <button @click="updateVol('volOrigMix', volOrigMix + 5)" class="step-btn">+</button>
             </div>
        </div>
        <div v-if="trackMode==='mix'" class="control-group">
             <label>Inst</label>
             <div class="stepper">
                <button @click="updateVol('volInstMix', volInstMix - 5)" class="step-btn">-</button>
                <div class="step-val">{{ volInstMix }}</div>
                <button @click="updateVol('volInstMix', volInstMix + 5)" class="step-btn">+</button>
             </div>
        </div>
    </div>

    <!-- Queue -->
    <div class="card queue">
        <h3>Queue</h3>
        <!-- Now Playing / Processing -->
        <div v-if="songs.length > 0" class="now-processing">
             <h4 v-if="songs[0].status === 'completed'">🎵 Now Playing: {{ songs[0].title || 'Unknown' }}</h4>
             <h4 v-else>⚙️ Processing: {{ songs[0].title || 'Unknown' }}</h4>
             
             <div class="status-bar" v-if="songs[0].status !== 'completed'">
                <div class="fill" :style="{width: songs[0].progress + '%'}"></div>
             </div>
             <p>{{ songs[0].status_detail || songs[0].status }}</p>
        </div>

        <!-- Pending List (Newest at bottom) -->
           <div class="queue-list">
               <div v-for="(song, index) in reversedQueue" :key="song.id" class="queue-item" :data-id="song.id">
                <div class="info">
                    <div class="title" :class="{ 'is-overflow': overflowMap[song.id]?.overflow }">
                        <span class="title-text" :style="overflowMap[song.id]?.overflow ? { '--marquee-distance': `-${overflowMap[song.id].distance}px` } : {}">
                            {{ song.title || song.url }}
                        </span>
                    </div>
                    <div class="status">
                        {{ song.status }}
                        <span v-if="song.status !== 'completed' && song.progress > 0">
                            {{ Math.round(song.progress) }}%
                        </span>
                        <span v-if="song.status_detail && song.status !== 'completed' && song.status !== 'pending'" style="font-size: 0.9em; color: #888;">
                             - {{ song.status_detail }}
                        </span>
                    </div>
                </div>
                <div class="actions">
                    <button class="btn-move" :disabled="index === 0" @click="moveSong(song.id, 'top')">置顶</button>
                    <button class="btn-del" @click="deleteSong(song.id)">✕</button>
                </div>
            </div>
        </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch, computed, nextTick } from 'vue'
import axios from 'axios'

// Dynamic Host Detection
const getHost = () => window.location.hostname
const API_BASE = `http://${getHost()}:8000`
const WS_BASE = `ws://${getHost()}:8000/ws/remote`

const isConnected = ref(false)
const newUrl = ref('')
const loading = ref(false)
const message = ref('')
const songs = ref([])
const overflowMap = ref({})

// State mirroring
const trackMode = ref('original')
const delay = ref(0)
const delayEnabled = ref(true)
const volOrigSolo = ref(100)
const volInstSolo = ref(100)
const volOrigMix = ref(80)
const volInstMix = ref(80)

let ws = null
let refreshInterval = null

// Computed
const reversedQueue = computed(() => {
    // DB returns [Playing, Pending1, Pending2...] (FIFO)
    // We want to show [Pending1, Pending2...] so Newest is at bottom.
    // If only 1 song (Playing), list is empty.
    if(songs.value.length === 0) return []
    
    // Check if the first song is the one actively playing/processing
    // Usually DB Index 0 is the "active" one
    // So we slice(1) to get the Queue
    return songs.value.slice(1)
})

watch(reversedQueue, () => nextTick(updateOverflow))

const updateOverflow = () => {
    const map = {}
    const items = document.querySelectorAll('.queue-list .queue-item')
    items.forEach((item) => {
        const id = item.getAttribute('data-id')
        const titleEl = item.querySelector('.title')
        if (!id || !titleEl) return
        const distance = Math.max(0, titleEl.scrollWidth - titleEl.clientWidth)
        const isOverflow = distance > 2
        map[id] = { overflow: isOverflow, distance }
    })
    overflowMap.value = map
}

// Load Locals & Server State
onMounted(async () => {
    // Fetch Server State first
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
    } catch(e) { console.log("State fetch failed, using local/defaults") }
    
    fetchSongs()
    refreshInterval = setInterval(fetchSongs, 2000)
    connectWS()
    window.addEventListener('resize', updateOverflow)
})

const connectWS = () => {
    ws = new WebSocket(WS_BASE)
    ws.onopen = () => isConnected.value = true
    ws.onmessage = (event) => {
        try {
            const cmd = JSON.parse(event.data)
            // Handle Sync from other Remotes
            if (cmd.action === 'setDelay') delay.value = Number(cmd.value)
            else if (cmd.action === 'setDelayEnabled') delayEnabled.value = !!cmd.value
            else if (cmd.action === 'setTrack') trackMode.value = cmd.value
            else if (cmd.action === 'setVolume') {
                const val = Number(cmd.value)
                const t = cmd.target
                if (t === 'volOrigSolo') volOrigSolo.value = val
                else if (t === 'volInstSolo') volInstSolo.value = val
                else if (t === 'volOrigMix') volOrigMix.value = val
                else if (t === 'volInstMix') volInstMix.value = val
            }
        } catch(e) {}
    }
    ws.onclose = () => {
        isConnected.value = false
        setTimeout(connectWS, 3000)
    }
}

const saveState = async (key, val) => {
    try {
        await axios.post(`${API_BASE}/state`, { key, value: String(val) })
    } catch(e) {}
}

const sendAction = (action, value=null, target=null) => {
    if(ws && ws.readyState === WebSocket.OPEN) {
        const payload = JSON.stringify({ action, value, target })
        ws.send(payload)
        console.log("Sent:", payload)
    } else {
        console.error("WS Not Connected", ws?.readyState)
        // Try reconnecting explicitly if action requested
        if(!ws || ws.readyState === WebSocket.CLOSED) connectWS()
    }
}

const setTrack = (mode) => {
    trackMode.value = mode
    saveState('trackMode', mode)
    sendAction('setTrack', mode)
}

const toggleDelay = () => {
    delayEnabled.value = !delayEnabled.value
    saveState('delayEnabled', delayEnabled.value ? '1' : '0')
    sendAction('setDelayEnabled', delayEnabled.value)
}

const updateDelay = (val) => {
    if (!delayEnabled.value) return
    delay.value = val
    saveState('delay', val)
    sendAction('setDelay', delay.value)
}

const updateVol = (target, val) => {
    // Clamp
    if (val < 0) val = 0;
    if (val > 100) val = 100;
    
    if (target === 'volOrigSolo') volOrigSolo.value = val
    if (target === 'volInstSolo') volInstSolo.value = val
    if (target === 'volOrigMix') volOrigMix.value = val
    if (target === 'volInstMix') volInstMix.value = val

    saveState(target, val)
    sendAction('setVolume', val, target)
}

// API
const fetchSongs = async () => {
    try {
        const res = await axios.get(`${API_BASE}/songs`)
        songs.value = res.data
        nextTick(updateOverflow)
    } catch (e) {}
}

const addSong = async () => {
    if (!newUrl.value) return
    loading.value = true
    try {
        await axios.post(`${API_BASE}/add_song`, { url: newUrl.value })
        newUrl.value = ''
        fetchSongs()
    } catch (e) { message.value = 'Failed to add' }
    loading.value = false
}

const moveSong = async (id, dir) => {
    try {
        await axios.post(`${API_BASE}/song/${id}/move`, { direction: dir })
        fetchSongs()
    } catch(e) {}
}

const deleteSong = async (id) => {
    if(confirm('Remove?')) await axios.delete(`${API_BASE}/song/${id}`); fetchSongs();
}

onUnmounted(() => {
    if (refreshInterval) clearInterval(refreshInterval)
    if (ws) ws.close()
    window.removeEventListener('resize', updateOverflow)
})
</script>

<style scoped>
.remote-view { padding: 15px; max-width: 600px; margin: 0 auto; padding-bottom: 50px; }
.queue-list .title {
    overflow: hidden;
    white-space: nowrap;
}
.queue-list .title-text {
    display: inline-block;
    transform: translateX(0);
}
.queue-list .title.is-overflow .title-text {
    animation: queue-marquee 12s linear infinite;
}
@keyframes queue-marquee {
    0% { transform: translateX(0); }
    20% { transform: translateX(0); }
    80% { transform: translateX(var(--marquee-distance, 0px)); }
    100% { transform: translateX(var(--marquee-distance, 0px)); }
}
.header { display: flex; justify-content: space-between; align-items: center; }
.connection-status { width: 10px; height: 10px; background: red; border-radius: 50%; }
.connection-status.connected { background: #0f0; box-shadow: 0 0 5px #0f0; }

.card { background: #222; padding: 15px; border-radius: 12px; margin-bottom: 25px; }
.add-box { display: flex; gap: 10px; }
.add-box input { flex: 1; padding: 12px; font-size:16px; border-radius: 6px; border: none; }
.add-box button { background: #1a73e8; color: white; border: none; padding: 0 20px; border-radius: 6px; font-weight: bold; }

/* Control Grids / Direct Layout */
.control-row-direct {
    display: flex;
    gap: 10px;
    margin-bottom: 15px;
}
.control-row-direct button {
    flex: 1; /* Force equal width */
}

/* Cleanup old grid styles if needed, or keep for safety */
.btn-main, .btn-warn {
    /* flex: 1;  Inherited from row */
    padding: 15px 5px; 
    font-size: 1.1em;
    border-radius: 8px;
    color: white;
    display: flex; justify-content: center; align-items: center;
    white-space: nowrap;
}
.btn-main { background: #333; border: 1px solid #444; }
.btn-warn { background: #600; border: none; }

.mode-select button { 
    padding: 15px 5px; 
    font-size: 1em; 
    background: #333; 
    border: 1px solid #444; 
    color: #888; 
    border-radius: 6px; 
    white-space: nowrap; 
}
.mode-select button.active { background: #1a73e8; color: white; border-color: #1a73e8; }

.control-group { margin-bottom: 25px; }
.stepper { display: flex; align-items: center; background: #333; border-radius: 8px; overflow: hidden; height: 50px; }
.step-btn { flex: 1; height: 100%; font-size: 1.5em; background: #444; color: white; border: none; }
.step-btn:active { background: #555; }
.step-val { width: 80px; text-align: center; font-size: 1.2em; font-weight: bold; }

.now-processing { background: #333; padding: 10px; border-radius: 8px; margin-bottom: 15px; }
.status-bar { height: 6px; background: #555; border-radius: 3px; overflow: hidden; margin: 10px 0; }
.fill { height: 100%; background: #1a73e8; transition: width 0.3s; }

.queue-item { display: flex; justify-content: space-between; align-items: center; padding: 15px 0; border-bottom: 1px solid #333; }
.info { flex: 1; overflow: hidden; margin-right: 10px; }
.title { font-size: 1.1em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.status { font-size: 0.8em; color: #aaa; }
.actions { display: flex; gap: 5px; }
.btn-move { background: #444; color: white; border: none; min-width: 56px; height: 40px; padding: 0 10px; border-radius: 4px; font-size: 0.95em; display: flex; align-items: center; justify-content: center; }
.btn-del { background: #600; color: white; border: none; width: 40px; height: 40px; border-radius: 4px; margin-left: 5px; font-size: 1.15em; display: flex; align-items: center; justify-content: center; line-height: 1; }
</style>
