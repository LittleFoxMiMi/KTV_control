<template>
  <div class="remote-view">
    <div v-show="activeTab === 'remote'" class="header">
      <h1>📱 KTV Remote</h1>
      <div class="connection-status" :class="{ connected: isConnected }"></div>
    </div>

    <div v-show="activeTab === 'remote'" class="tab-page">
      <div class="card add-box">
        <input v-model="newUrl" placeholder="Bilibili / YouTube URL" @keyup.enter="addSong" />
        <button @click="addSong" :disabled="loading">Add</button>
      </div>

      <div class="control-row-direct">
        <button class="btn-warn" @click="sendAction('skip')">⏭ Skip</button>
        <button class="btn-main" @click="sendAction('pause')">⏯ Play/Pause</button>
        <button class="btn-main" @click="sendAction('restart')">↺ Restart</button>
      </div>

      <div class="control-row-direct mode-select">
        <button @click="setTrack('original')" :class="{ active: trackMode === 'original' }">Vocals</button>
        <button @click="setTrack('instrumental')" :class="{ active: trackMode === 'instrumental' }">Instr.</button>
        <button @click="setTrack('mix')" :class="{ active: trackMode === 'mix' }">Mix</button>
        <button @click="toggleDelay" :class="{ active: delayEnabled }">Delay</button>
      </div>

      <div class="card settings-box">
        <div class="control-group">
          <label>Delay: {{ delayEnabled ? delay : 0 }}ms</label>
          <div class="stepper">
            <button @click="updateDelay(delay - 50)" class="step-btn" :disabled="!delayEnabled">-</button>
            <div class="step-val">{{ delay }}</div>
            <button @click="updateDelay(delay + 50)" class="step-btn" :disabled="!delayEnabled">+</button>
          </div>
        </div>

        <div v-if="trackMode === 'original'" class="control-group">
          <label>Vocals Vol</label>
          <div class="stepper">
            <button @click="updateVol('volOrigSolo', volOrigSolo - 5)" class="step-btn">-</button>
            <div class="step-val">{{ volOrigSolo }}</div>
            <button @click="updateVol('volOrigSolo', volOrigSolo + 5)" class="step-btn">+</button>
          </div>
        </div>

        <div v-if="trackMode === 'instrumental'" class="control-group">
          <label>Inst Vol</label>
          <div class="stepper">
            <button @click="updateVol('volInstSolo', volInstSolo - 5)" class="step-btn">-</button>
            <div class="step-val">{{ volInstSolo }}</div>
            <button @click="updateVol('volInstSolo', volInstSolo + 5)" class="step-btn">+</button>
          </div>
        </div>

        <div v-if="trackMode === 'mix'" class="control-group">
          <label>Vocals</label>
          <div class="stepper">
            <button @click="updateVol('volOrigMix', volOrigMix - 5)" class="step-btn">-</button>
            <div class="step-val">{{ volOrigMix }}</div>
            <button @click="updateVol('volOrigMix', volOrigMix + 5)" class="step-btn">+</button>
          </div>
        </div>

        <div v-if="trackMode === 'mix'" class="control-group">
          <label>Inst</label>
          <div class="stepper">
            <button @click="updateVol('volInstMix', volInstMix - 5)" class="step-btn">-</button>
            <div class="step-val">{{ volInstMix }}</div>
            <button @click="updateVol('volInstMix', volInstMix + 5)" class="step-btn">+</button>
          </div>
        </div>
      </div>
    </div>

    <div v-show="activeTab === 'search'" class="tab-page search-page">
      <div v-if="noticeMessage" class="notice-bar" :class="noticeType === 'error' ? 'is-error' : 'is-info'">{{ noticeMessage }}</div>
      <div class="search-top card">
        <input
          v-model="musicKeyword"
          placeholder="输入歌曲名/歌手"
          @keyup.enter="searchLocal"
        />
        <button @click="searchLocal" :disabled="searchLoading">搜索</button>
      </div>

      <div class="search-status">{{ searchMessage }}</div>
      <div v-if="searchProgressVisible" class="search-center-progress">
        <template v-if="showDeterminateProgress">
          <div class="progress-ring" :style="ringStyle">
            <div class="progress-ring-inner">{{ Math.round(searchProgress) }}%</div>
          </div>
        </template>
        <template v-else>
          <div class="spinner-ring"></div>
          <div class="search-progress-hint">正在搜索，通常约 20s</div>
        </template>
        <button v-if="searchRunning" class="btn-cancel center-cancel" @click="cancelSearch">取消搜索</button>
        <div class="search-progress-text" v-if="showDeterminateProgress && searchTotalSources > 0">
          总进度 {{ searchCompletedSources }}/{{ searchTotalSources }}
        </div>
      </div>

      <div class="search-list">
        <div v-for="item in mergedSearchRecords" :key="item._key" class="song-item" :data-skey="item._key">
          <div class="song-main">
            <div class="song-top-row">
              <div class="title" :class="{ 'is-overflow': searchOverflowMap[item._key]?.overflow }">
                <span class="title-text" :style="searchOverflowMap[item._key]?.overflow ? { '--marquee-distance': `-${searchOverflowMap[item._key].distance}px` } : {}">
                  {{ item.song_name || 'Unknown' }}
                </span>
              </div>
              <div class="side-meta">
                <div class="singer">{{ item.singers || '-' }}</div>
                <div class="album">{{ item.album || '-' }}</div>
              </div>
            </div>
            <div class="song-bottom-row">
              <span>{{ toCnSource(item.source) }}</span>
              <span>{{ (item.ext || '-').toUpperCase() }} · {{ item.file_size || '-' }}</span>
            </div>
            <span v-if="item.from_library" class="lib-badge">来自歌曲库</span>
          </div>
          <div class="actions">
            <button v-if="item.from_library" @click="queueFromLibrary(item)">恢复</button>
            <button v-else @click="queueFromSearch(item)">加入队列</button>
          </div>
        </div>

        <div v-if="showJBSearchEntry" class="search-entry search-action" @click="startJBSearch">
          🔎 点击进行网络搜索（JBSou）
        </div>
        <div v-if="showThreeSearchEntry" class="search-entry search-action" @click="startNormalSearch">
          ⏱ 点击进行三平台搜索（网易/酷我/酷狗，约 20s）
        </div>
        <div v-if="showQQSearchEntry" class="search-entry search-action" @click="startQQSearch">
          🎵 QQ 音乐单独执行（约 1 分钟），点击开始 QQ 搜索
        </div>
      </div>
    </div>

    <div v-show="activeTab === 'queue'" class="tab-page">
      <div class="card queue">
        <h3>Queue</h3>
        <div v-if="songs.length > 0" class="now-processing">
          <h4 v-if="songs[0].status === 'completed'">🎵 Now Playing: {{ songs[0].title || 'Unknown' }}</h4>
          <h4 v-else>⚙️ Processing: {{ songs[0].title || 'Unknown' }}</h4>

          <div class="status-bar" v-if="songs[0].status !== 'completed'">
            <div class="fill" :style="{ width: songs[0].progress + '%' }"></div>
          </div>
          <p>{{ songs[0].status_detail || songs[0].status }}</p>
        </div>

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
                <span v-if="song.status !== 'completed' && song.progress > 0">{{ Math.round(song.progress) }}%</span>
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

    <div class="bottom-tabs">
      <button :class="{ active: activeTab === 'remote' }" @click="activeTab = 'remote'">遥控</button>
      <button :class="{ active: activeTab === 'search' }" @click="activeTab = 'search'">搜索</button>
      <button :class="{ active: activeTab === 'queue' }" @click="activeTab = 'queue'">队列</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch, computed, nextTick } from 'vue'
import axios from 'axios'

const getHost = () => window.location.hostname
const API_BASE = `http://${getHost()}:8000`
const WS_BASE = `ws://${getHost()}:8000/ws/remote`

const activeTab = ref('remote')
const isConnected = ref(false)
const newUrl = ref('')
const loading = ref(false)
const songs = ref([])
const overflowMap = ref({})
const searchOverflowMap = ref({})
const noticeMessage = ref('')
const noticeType = ref('info')
let noticeTimer = null

const trackMode = ref('original')
const delay = ref(0)
const delayEnabled = ref(true)
const volOrigSolo = ref(100)
const volInstSolo = ref(100)
const volOrigMix = ref(80)
const volInstMix = ref(80)

const musicKeyword = ref('')
const localSearchRecords = ref([])
const remoteSearchRecords = ref([])
const searchMessage = ref('输入关键词后先本地搜索，命中歌曲库将直接展示。')
const searchLoading = ref(false)
const searchRunning = ref(false)
const currentSearchJobId = ref('')
const currentSearchMode = ref('')
const jbSearchDone = ref(false)
const normalSearchDone = ref(false)
const searchProgress = ref(0)
const searchTotalSources = ref(0)
const searchCompletedSources = ref(0)
const searchProgressVisible = ref(false)
const hasSearchedLocal = ref(false)
let searchPollTimer = null

let ws = null
let refreshInterval = null
const onResize = () => {
  updateOverflow()
  updateSearchOverflow()
}

const reversedQueue = computed(() => {
  if (songs.value.length === 0) return []
  return songs.value.slice(1)
})

const mergedSearchRecords = computed(() => {
  const local = localSearchRecords.value.map((item) => ({
    ...item,
    _key: `local-${item.library_id ?? item.id}`,
  }))
  const remote = remoteSearchRecords.value.map((item) => ({
    ...item,
    _key: `remote-${item._job_id}-${item.id}`,
  }))
  return [...local, ...remote]
})

const showJBSearchEntry = computed(() => hasSearchedLocal.value && !searchRunning.value && !jbSearchDone.value)
const showThreeSearchEntry = computed(() => hasSearchedLocal.value && jbSearchDone.value && !searchRunning.value && !normalSearchDone.value)
const showQQSearchEntry = computed(() => hasSearchedLocal.value && normalSearchDone.value && !searchRunning.value)
const ringStyle = computed(() => {
  const pct = Math.max(0, Math.min(100, Number(searchProgress.value || 0)))
  return {
    background: `conic-gradient(#4f8bff 0% ${pct}%, #2f2f2f ${pct}% 100%)`,
  }
})
const showDeterminateProgress = computed(() => Number(searchProgress.value || 0) > 0)

const showNotice = (msg, type = 'info') => {
  noticeMessage.value = msg
  noticeType.value = type
  if (noticeTimer) clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => {
    noticeMessage.value = ''
    noticeType.value = 'info'
    noticeTimer = null
  }, 3500)
}

const isNoFileError = (e) => {
  const detail = String(e?.response?.data?.detail || '').toLowerCase()
  const message = String(e?.message || '').toLowerCase()
  return detail.includes('no file') || message.includes('no file')
}

watch(reversedQueue, () => nextTick(updateOverflow))
watch(mergedSearchRecords, () => nextTick(updateSearchOverflow))

const updateOverflow = () => {
  const map = {}
  const items = document.querySelectorAll('.queue-list .queue-item')
  items.forEach((item) => {
    const id = item.getAttribute('data-id')
    const titleEl = item.querySelector('.title')
    if (!id || !titleEl) return
    const distance = Math.max(0, titleEl.scrollWidth - titleEl.clientWidth)
    map[id] = { overflow: distance > 2, distance }
  })
  overflowMap.value = map
}

const updateSearchOverflow = () => {
  const map = {}
  const items = document.querySelectorAll('.search-list .song-item')
  items.forEach((item) => {
    const key = item.getAttribute('data-skey')
    const titleEl = item.querySelector('.song-top-row .title')
    if (!key || !titleEl) return
    const distance = Math.max(0, titleEl.scrollWidth - titleEl.clientWidth)
    map[key] = { overflow: distance > 2, distance }
  })
  searchOverflowMap.value = map
}

const toCnSource = (source) => {
  const mapping = {
    JBSouMusicClient: 'JBSou',
    QQMusicClient: 'QQ音乐',
    NeteaseMusicClient: '网易云',
    KuwoMusicClient: '酷我',
    KugouMusicClient: '酷狗',
    jbsou: 'JBSou',
    qq: 'QQ音乐',
    netease: '网易云',
    kuwo: '酷我',
    kugou: '酷狗',
  }
  return mapping[source] || source || '-'
}

const connectWS = () => {
  ws = new WebSocket(WS_BASE)
  ws.onopen = () => (isConnected.value = true)
  ws.onmessage = (event) => {
    try {
      const cmd = JSON.parse(event.data)
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
    } catch (e) {}
  }
  ws.onclose = () => {
    isConnected.value = false
    setTimeout(connectWS, 3000)
  }
}

const saveState = async (key, val) => {
  try {
    await axios.post(`${API_BASE}/state`, { key, value: String(val) })
  } catch (e) {}
}

const sendAction = (action, value = null, target = null) => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action, value, target }))
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
  const v = Math.max(0, Math.min(100, val))
  if (target === 'volOrigSolo') volOrigSolo.value = v
  if (target === 'volInstSolo') volInstSolo.value = v
  if (target === 'volOrigMix') volOrigMix.value = v
  if (target === 'volInstMix') volInstMix.value = v
  saveState(target, v)
  sendAction('setVolume', v, target)
}

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
  } finally {
    loading.value = false
  }
}

const moveSong = async (id, dir) => {
  try {
    await axios.post(`${API_BASE}/song/${id}/move`, { direction: dir })
    fetchSongs()
  } catch (e) {}
}

const deleteSong = async (id) => {
  if (!confirm('Remove?')) return
  await axios.delete(`${API_BASE}/song/${id}`)
  fetchSongs()
}

const stopSearchPolling = () => {
  if (searchPollTimer) {
    clearInterval(searchPollTimer)
    searchPollTimer = null
  }
}

const cancelCurrentSearchIfAny = async () => {
  const jobId = currentSearchJobId.value
  if (!jobId || !searchRunning.value) return
  try {
    await axios.post(`${API_BASE}/music/search/job/${jobId}/cancel`)
  } catch (e) {}
  stopSearchPolling()
  searchRunning.value = false
  searchProgressVisible.value = false
  searchProgress.value = 0
  searchTotalSources.value = 0
  searchCompletedSources.value = 0
}

const searchLocal = async () => {
  const kw = musicKeyword.value.trim()
  if (!kw) return
  await cancelCurrentSearchIfAny()
  hasSearchedLocal.value = true
  searchLoading.value = true
  searchMessage.value = '正在查询本地歌曲库...'
  searchProgress.value = 0
  searchTotalSources.value = 0
  searchCompletedSources.value = 0
  searchProgressVisible.value = false
  jbSearchDone.value = false
  normalSearchDone.value = false
  currentSearchMode.value = ''
  currentSearchJobId.value = ''
  stopSearchPolling()
  remoteSearchRecords.value = []
  try {
    const res = await axios.get(`${API_BASE}/music/search/local`, { params: { keyword: kw } })
    localSearchRecords.value = (res.data.records || []).map((x) => ({ ...x, from_library: true }))
    if (localSearchRecords.value.length > 0) {
      searchMessage.value = `命中歌曲库 ${localSearchRecords.value.length} 条，可继续 JBSou 网络搜索。`
    } else {
      searchMessage.value = '本地未命中，正在进行 JBSou 网络搜索...'
      await startNetworkSearch('jb')
    }
  } catch (e) {
    searchMessage.value = '本地搜索失败'
  } finally {
    searchLoading.value = false
  }
}

const startNetworkSearch = async (mode) => {
  const kw = musicKeyword.value.trim()
  if (!kw) return
  await cancelCurrentSearchIfAny()
  searchRunning.value = true
  if (mode === 'jb') {
    searchMessage.value = 'JBSou 搜索任务已提交，正在排队/执行...'
  } else if (mode === 'qq') {
    searchMessage.value = 'QQ 搜索任务已提交，正在排队/执行...'
  } else {
    searchMessage.value = '三平台搜索任务已提交（约20s），正在排队/执行...'
  }
  searchProgress.value = 0
  searchTotalSources.value = mode === 'normal' ? 3 : 1
  searchCompletedSources.value = 0
  searchProgressVisible.value = true
  currentSearchMode.value = mode
  if (mode === 'jb') {
    jbSearchDone.value = false
    remoteSearchRecords.value = []
  }
  if (mode === 'normal') {
    normalSearchDone.value = false
  }
  try {
    const res = await axios.post(`${API_BASE}/music/search/start`, { keyword: kw, mode })
    currentSearchJobId.value = res.data.job_id
    searchMessage.value = res.data.ahead > 0 ? `排队中，前方还有 ${res.data.ahead} 个任务` : '正在搜索...'
    stopSearchPolling()
    searchPollTimer = setInterval(pollSearchJob, 1200)
  } catch (e) {
    searchRunning.value = false
    searchMessage.value = '提交搜索失败'
  }
}

const startNormalSearch = async () => startNetworkSearch('normal')
const startJBSearch = async () => startNetworkSearch('jb')
const startQQSearch = async () => startNetworkSearch('qq')

const pollSearchJob = async () => {
  const jobId = currentSearchJobId.value
  if (!jobId) return
  try {
    const res = await axios.get(`${API_BASE}/music/search/job/${jobId}`)
    const data = res.data
    searchMessage.value = data.status === 'waiting' ? `排队中，前方还有 ${data.ahead} 个任务` : data.message
    searchProgress.value = Number(data.progress || 0)
    searchTotalSources.value = Number(data.total_sources || 0)
    searchCompletedSources.value = Number(data.completed_sources || 0)
    if (data.status === 'done') {
      const mapped = (data.records || []).map((x) => ({ ...x, _job_id: data.job_id, from_library: false }))
      if (data.mode === 'jb') {
        remoteSearchRecords.value = mapped
        jbSearchDone.value = true
      } else if (data.mode === 'normal') {
        remoteSearchRecords.value = mapped
        normalSearchDone.value = true
      } else {
        remoteSearchRecords.value = [...remoteSearchRecords.value, ...mapped]
      }
      searchRunning.value = false
      stopSearchPolling()
      searchProgressVisible.value = false
      searchProgress.value = 0
      searchTotalSources.value = 0
      searchCompletedSources.value = 0
    } else if (data.status === 'cancelled' || data.status === 'error') {
      searchRunning.value = false
      stopSearchPolling()
      searchProgressVisible.value = false
      searchProgress.value = 0
      searchTotalSources.value = 0
      searchCompletedSources.value = 0
    }
  } catch (e) {
    searchRunning.value = false
    stopSearchPolling()
    searchMessage.value = '搜索状态查询失败'
    searchProgressVisible.value = false
    searchProgress.value = 0
    searchTotalSources.value = 0
    searchCompletedSources.value = 0
  }
}

const cancelSearch = async () => {
  if (!currentSearchJobId.value) return
  try {
    await axios.post(`${API_BASE}/music/search/job/${currentSearchJobId.value}/cancel`)
    searchMessage.value = '已取消搜索'
    searchRunning.value = false
    stopSearchPolling()
    searchProgressVisible.value = false
    searchProgress.value = 0
    searchTotalSources.value = 0
    searchCompletedSources.value = 0
  } catch (e) {
    searchMessage.value = '取消失败'
  }
}

const queueFromLibrary = async (item) => {
  try {
    if ((item.library_type || 'music') === 'mv') {
      await axios.post(`${API_BASE}/music/queue/from_mv_library`, { video_id: item.video_id })
    } else {
      await axios.post(`${API_BASE}/music/queue/from_library`, { library_id: item.library_id })
    }
    fetchSongs()
    activeTab.value = 'queue'
  } catch (e) {
    if (isNoFileError(e)) {
      showNotice('文件不存在（no file），该条目已失效并已移除。', 'error')
    } else {
      showNotice('加入队列失败，请稍后重试。', 'error')
    }
    fetchSongs()
  }
}

const queueFromSearch = async (item) => {
  if (!item._job_id) return
  try {
    await axios.post(`${API_BASE}/music/queue/from_search`, { job_id: item._job_id, result_id: item.id })
    fetchSongs()
    activeTab.value = 'queue'
  } catch (e) {
    if (isNoFileError(e)) {
      showNotice('文件不存在（no file），队列条目已自动移除。', 'error')
    } else {
      showNotice('加入队列失败，请稍后重试。', 'error')
    }
    fetchSongs()
  }
}

onMounted(async () => {
  try {
    const res = await axios.get(`${API_BASE}/state`)
    const s = res.data
    if (s.trackMode) trackMode.value = s.trackMode
    if (s.delay) delay.value = Number(s.delay)
    if (s.delayEnabled !== undefined) delayEnabled.value = s.delayEnabled === '1' || s.delayEnabled === 'true'
    if (s.volOrigSolo) volOrigSolo.value = Number(s.volOrigSolo)
    if (s.volInstSolo) volInstSolo.value = Number(s.volInstSolo)
    if (s.volOrigMix) volOrigMix.value = Number(s.volOrigMix)
    if (s.volInstMix) volInstMix.value = Number(s.volInstMix)
  } catch (e) {}

  fetchSongs()
  refreshInterval = setInterval(fetchSongs, 2000)
  connectWS()
  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  if (refreshInterval) clearInterval(refreshInterval)
  if (ws) ws.close()
  stopSearchPolling()
  if (noticeTimer) clearTimeout(noticeTimer)
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.remote-view { max-width: 680px; margin: 0 auto; padding: 12px 12px 90px; }
.header { display: flex; justify-content: space-between; align-items: center; }
.header h1 { margin: 0; font-size: 1.5em; line-height: 1.1; }
.connection-status { width: 10px; height: 10px; background: red; border-radius: 50%; }
.connection-status.connected { background: #0f0; box-shadow: 0 0 5px #0f0; }
.tab-page { margin-top: 10px; }
.card { background: #222; padding: 12px; border-radius: 10px; margin-bottom: 12px; }
.add-box { display: flex; gap: 8px; }
.add-box input { flex: 1; padding: 10px; border-radius: 6px; border: none; }
.add-box button { border: none; border-radius: 6px; padding: 0 14px; background: #1a73e8; color: #fff; }
.control-row-direct { display: flex; gap: 8px; margin-bottom: 10px; }
.control-row-direct button { flex: 1; padding: 16px 8px; border: none; border-radius: 10px; color: #fff; font-size: 16px; font-weight: 700; }
.btn-main { background: #1a73e8; }
.btn-warn { background: #c66a00; }
.mode-select button.active { background: #4e7dff; }
.settings-box .control-group { margin-bottom: 10px; width: 100%; display: block; }
.settings-box .control-group label { display: block; margin-bottom: 8px; font-size: 15px; }
.stepper { display: flex; align-items: center; gap: 8px; width: 100%; }
.step-btn {
  width: 52px;
  height: 52px;
  border: none;
  border-radius: 10px;
  background: #2f2f2f;
  color: #fff;
  font-size: 26px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
  padding: 0;
}
.step-val { flex: 1; min-width: 38px; text-align: center; background: #1a1a1a; border-radius: 10px; padding: 12px 0; font-size: 18px; font-weight: 700; }

.search-page { height: calc(100vh - 170px); display: flex; flex-direction: column; }
.notice-bar {
  margin-bottom: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12px;
}
.notice-bar.is-error {
  background: #4a2424;
  border: 1px solid #7a3c3c;
  color: #ffd3d3;
}
.notice-bar.is-info {
  background: #233247;
  border: 1px solid #37547a;
  color: #d6e7ff;
}
.search-top { position: sticky; top: 0; z-index: 3; display: flex; gap: 8px; margin-bottom: 8px; }
.search-top input { flex: 1; padding: 10px; border: none; border-radius: 6px; }
.search-top button { border: none; border-radius: 6px; padding: 0 12px; background: #1a73e8; color: #fff; }
.search-top .btn-cancel { background: #b23a3a; }
.search-status { font-size: 12px; color: #bbb; margin-bottom: 8px; }
.search-center-progress { margin-bottom: 10px; display: flex; flex-direction: column; align-items: center; }
.progress-ring {
  width: 112px;
  height: 112px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.progress-ring-inner {
  width: 84px;
  height: 84px;
  border-radius: 50%;
  background: #1a1a1a;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 700;
}
.spinner-ring {
  width: 88px;
  height: 88px;
  border-radius: 50%;
  border: 8px solid #2f2f2f;
  border-top-color: #4f8bff;
  animation: spin-ring 0.9s linear infinite;
}
.search-progress-hint { margin-top: 8px; font-size: 12px; color: #bdbdbd; }
.center-cancel { margin-top: 10px; border: none; border-radius: 8px; padding: 8px 16px; background: #b23a3a; color: #fff; }
.search-progress-text { font-size: 12px; color: #9a9a9a; margin-top: 6px; }
.search-list { overflow-y: auto; padding-bottom: 20px; }
.song-item { background: #1f1f1f; border: 1px solid #333; border-radius: 8px; padding: 10px; margin-bottom: 8px; display: flex; gap: 10px; align-items: center; }
.song-main { flex: 1; min-width: 0; }
.song-top-row { display: flex; align-items: stretch; min-height: 48px; }
.song-top-row .title {
  flex: 0 0 66.666%;
  max-width: 66.666%;
  overflow: hidden;
  white-space: nowrap;
  font-size: 22px;
  font-weight: 800;
  display: flex;
  align-items: center;
}
.song-top-row .title-text { display: inline-block; transform: translateX(0); }
.song-top-row .title.is-overflow .title-text { animation: queue-marquee 12s linear infinite; }
.side-meta {
  flex: 0 0 33.334%;
  max-width: 33.334%;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: flex-end;
  min-height: 48px;
  color: #d8d8d8;
  font-size: 13px;
  line-height: 1.2;
  padding-left: 8px;
  overflow: hidden;
}
.side-meta .singer, .side-meta .album { width: 100%; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.song-bottom-row { margin-top: 6px; font-size: 12px; color: #9c9c9c; display: flex; justify-content: space-between; }
.song-item .actions { margin-top: 0; flex: 0 0 auto; }
.song-item .actions button { border: none; border-radius: 6px; padding: 6px 12px; background: #2f6fd6; color: #fff; }
.lib-badge { font-size: 11px; color: #9df0a8; border: 1px solid #4f8c5a; border-radius: 999px; padding: 2px 6px; }
.search-entry { background: #1d1d1d; border: 1px dashed #3d3d3d; border-radius: 8px; padding: 10px; margin-bottom: 8px; font-size: 13px; }
.search-action { cursor: pointer; }
.search-action:hover { background: #252525; }
.search-hint { color: #c7b58a; }

.queue-list .queue-item { display: flex; justify-content: space-between; gap: 8px; padding: 8px 0; border-bottom: 1px solid #333; }
.queue-list .actions {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
  white-space: nowrap;
}
.queue-list .title { max-width: 300px; overflow: hidden; white-space: nowrap; }
.queue-list .title-text { display: inline-block; transform: translateX(0); }
.queue-list .title.is-overflow .title-text { animation: queue-marquee 12s linear infinite; }
@keyframes queue-marquee {
  0% { transform: translateX(0); }
  20% { transform: translateX(0); }
  80% { transform: translateX(var(--marquee-distance, 0px)); }
  100% { transform: translateX(var(--marquee-distance, 0px)); }
}
@keyframes spin-ring {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.status-bar { width: 100%; height: 6px; background: #333; border-radius: 999px; overflow: hidden; margin: 6px 0; }
.fill { height: 100%; background: #4f8bff; }
.btn-move, .btn-del { border: none; border-radius: 6px; padding: 8px 12px; color: #fff; }
.btn-move { background: #4463a8; }
.btn-del { background: #963d3d; }

.bottom-tabs {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  height: 62px;
  background: #171717;
  border-top: 1px solid #333;
  display: flex;
  z-index: 20;
}
.bottom-tabs button {
  flex: 1;
  border: none;
  background: transparent;
  color: #aaa;
  font-size: 15px;
}
.bottom-tabs button.active { color: #fff; background: #262626; }
</style>
