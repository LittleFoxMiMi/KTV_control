import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import PlayerView from '../views/PlayerView.vue'
import RemoteView from '../views/RemoteView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView
    },
    {
      path: '/player',
      name: 'player',
      component: PlayerView
    },
    {
      path: '/remote',
      name: 'remote',
      component: RemoteView
    }
  ]
})

export default router
