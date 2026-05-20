<script setup lang="ts">
import { onMounted, ref } from "vue";
import { client } from "../api/client";

const templates = ref<any[]>([]);
const err = ref<string>("");

onMounted(async () => {
  try {
    templates.value = (await client().get("/admin/personas/templates")).data;
  } catch (e: any) {
    err.value = e.message;
  }
});
</script>

<template>
  <h2>Persona templates</h2>
  <p v-if="err" class="err">{{ err }}</p>
  <div v-for="t in templates" :key="t.template_id" class="card">
    <h3>{{ t.name }} <small>({{ t.template_id }} v{{ t.version }})</small></h3>
    <p>{{ t.description }}</p>
    <p><strong>Archetype:</strong> {{ t.archetype }}</p>
    <p><strong>Taboos:</strong> {{ (t.taboos ?? []).join(", ") }}</p>
    <p><strong>Locked fields:</strong> {{ (t.locked_fields ?? []).join(", ") }}</p>
  </div>
</template>

<style scoped>
.card { border: 1px solid #ddd; padding: 1rem; margin-bottom: 1rem; border-radius: .4rem; }
.err { color: crimson; }
</style>
