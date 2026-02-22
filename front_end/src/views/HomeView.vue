<template>
  <div class="home-container">
    <h1>KTV System</h1>
    <div class="selection">
      <router-link to="/player" class="card-btn">
        <h2>📺 Enter Player Mode</h2>
        <p>For TV / Big Screen</p>
      </router-link>
      
      <router-link to="/remote" class="card-btn">
        <h2>📱 Enter Remote Mode</h2>
        <p>For Phone / Controller</p>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.home-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  min-height: 100vh;
  padding: 24px 16px;
  box-sizing: border-box;
    text-align: center;
}
.selection {
    display: flex;
  flex-wrap: wrap;
  justify-content: center;
    gap: 20px;
    margin-top: 30px;
  width: 100%;
  max-width: 760px;
}
.card-btn {
    display: block;
    background: #333;
    padding: 30px;
    border-radius: 12px;
    text-decoration: none;
    color: white;
  width: 260px;
  max-width: 100%;
  box-sizing: border-box;
    transition: transform 0.2s;
}
.card-btn:hover {
    transform: scale(1.05);
    background: #444;
}
h2 { margin: 0 0 10px 0; font-size: 1.5em; }
p { margin: 0; color: #aaa; }

@media (max-width: 768px) {
  .home-container {
    justify-content: flex-start;
    padding-top: 32px;
  }

  .selection {
    flex-direction: column;
    align-items: center;
    gap: 14px;
  }

  .card-btn {
    width: min(100%, 360px);
    padding: 22px 18px;
  }

  h2 {
    font-size: 1.2em;
  }
}
</style>
