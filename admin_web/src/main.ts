import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";

import App from "./App.vue";
import ChatTest from "./pages/ChatTest.vue";
import Dashboard from "./pages/Dashboard.vue";
import Devices from "./pages/Devices.vue";
import PersonaInstanceDetail from "./pages/PersonaInstanceDetail.vue";
import PersonaInstances from "./pages/PersonaInstances.vue";
import PersonaTemplateDetail from "./pages/PersonaTemplateDetail.vue";
import PersonaTemplates from "./pages/PersonaTemplates.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: Dashboard },
    { path: "/devices", component: Devices },
    { path: "/templates", component: PersonaTemplates },
    { path: "/templates/:id", component: PersonaTemplateDetail },
    { path: "/instances", component: PersonaInstances },
    {
      path: "/instances/:tenant/:user/:instance",
      component: PersonaInstanceDetail,
    },
    { path: "/chat-test", component: ChatTest },
  ],
});

createApp(App).use(createPinia()).use(router).mount("#app");
