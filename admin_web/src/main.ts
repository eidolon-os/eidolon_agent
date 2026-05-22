import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";

import App from "./App.vue";
import ChatTest from "./pages/ChatTest.vue";
import Dashboard from "./pages/Dashboard.vue";
import Devices from "./pages/Devices.vue";
import PersonaTemplates from "./pages/PersonaTemplates.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: Dashboard },
    { path: "/devices", component: Devices },
    { path: "/templates", component: PersonaTemplates },
    { path: "/chat-test", component: ChatTest },
  ],
});

createApp(App).use(createPinia()).use(router).mount("#app");
