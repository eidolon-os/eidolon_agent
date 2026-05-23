<script setup lang="ts">
import { onMounted, ref } from "vue";
import { client } from "../api/client";

const templates = ref<any[]>([]);
const err = ref<string>("");
const reloading = ref(false);

async function refresh() {
  try {
    templates.value = (await client().get("/admin/personas/templates")).data;
  } catch (e: any) {
    err.value = e.message;
  }
}

async function reload() {
  reloading.value = true;
  try {
    await client().post("/admin/personas/templates/reload");
    await refresh();
  } catch (e: any) {
    err.value = e.message;
  } finally {
    reloading.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <h2>
    Persona templates
    <button :disabled="reloading" class="reload" @click="reload">
      {{ reloading ? "Reloading…" : "Reload from disk" }}
    </button>
  </h2>
  <p v-if="err" class="err">{{ err }}</p>
  <table class="grid">
    <thead>
      <tr>
        <th>Template</th>
        <th>Archetype</th>
        <th>Revision</th>
        <th>Description</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="t in templates" :key="t.template_id">
        <td><strong>{{ t.name }}</strong><br><small>{{ t.template_id }}</small></td>
        <td>{{ t.archetype }}</td>
        <td>v{{ t.template_revision }}</td>
        <td>{{ t.description }}</td>
        <td><router-link :to="`/templates/${t.template_id}`">Details →</router-link></td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.reload { margin-left: 1rem; font-size: 0.9rem; }
.grid { width: 100%; border-collapse: collapse; }
.grid th, .grid td { padding: .5rem .75rem; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }
.err { color: crimson; }
</style>
