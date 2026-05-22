<template>
  <header>
    <h1>Eidolon Agent Admin</h1>
    <nav>
      <router-link to="/">Dashboard</router-link> |
      <router-link to="/devices">Devices</router-link> |
      <router-link to="/templates">Templates</router-link> |
      <router-link to="/chat-test">Chat Test</router-link>
    </nav>
  </header>
  <main>
    <router-view />
  </main>
</template>

<style>
body { font-family: -apple-system, system-ui, sans-serif; margin: 0; }
header { padding: 1rem 1.5rem; border-bottom: 1px solid #eee; }
nav { margin-top: .5rem; }
main { padding: 1.5rem; }
</style>
