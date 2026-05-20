<script setup lang="ts">
import { onMounted, ref } from "vue";
import { client } from "../api/client";

const health = ref<unknown>(null);
const error = ref<string>("");

onMounted(async () => {
  try {
    const { data } = await client().get("/admin/personas/templates");
    health.value = data;
  } catch (e: any) {
    error.value = e.message ?? String(e);
  }
});
</script>

<template>
  <h2>Dashboard</h2>
  <p v-if="error" class="err">{{ error }}</p>
  <pre v-else>{{ JSON.stringify(health, null, 2) }}</pre>
</template>

<style>
.err { color: crimson; }
pre { background: #f7f7f7; padding: 1rem; overflow: auto; }
</style>
