import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";

import App from "./App.vue";
import Dashboard from "./pages/Dashboard.vue";
import Devices from "./pages/Devices.vue";
import AgentInstances from "./pages/AgentInstances.vue";
import PersonaTemplates from "./pages/PersonaTemplates.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: Dashboard },
    { path: "/devices", component: Devices },
    { path: "/instances", component: AgentInstances },
    { path: "/templates", component: PersonaTemplates },
  ],
});

createApp(App).use(createPinia()).use(router).mount("#app");
