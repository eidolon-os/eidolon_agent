<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api/client";

const route = useRoute();
const templateId = String(route.params.id);
const tpl = ref<any | null>(null);
const raw = ref<string>("");
const err = ref<string>("");
const showRaw = ref(false);

onMounted(async () => {
  try {
    tpl.value = (await client().get(`/admin/personas/templates/${templateId}`)).data;
    raw.value = (await client().get(`/admin/personas/templates/${templateId}/raw`)).data;
  } catch (e: any) {
    err.value = e.message;
  }
});
</script>

<template>
  <p><router-link to="/templates">← Back to templates</router-link></p>
  <p v-if="err" class="err">{{ err }}</p>
  <div v-if="tpl">
    <h2>{{ tpl.metadata.name }} <small>({{ tpl.metadata.template_id }} v{{ tpl.metadata.template_revision }})</small></h2>
    <p><strong>Archetype:</strong> {{ tpl.metadata.archetype }}</p>
    <p v-if="tpl.metadata.description">{{ tpl.metadata.description }}</p>

    <h3>Identity core</h3>
    <ul>
      <li><strong>Base pronouns:</strong> {{ tpl.identity_core.base_pronouns }}</li>
      <li><strong>Values:</strong> {{ (tpl.identity_core.values || []).join("、") || "（无）" }}</li>
      <li><strong>Unbreakable rules:</strong> {{ (tpl.identity_core.unbreakable_rules || []).join("；") || "（无）" }}</li>
      <li><strong>Taboos:</strong> {{ (tpl.identity_core.taboos || []).join("、") || "（无）" }}</li>
    </ul>

    <h3>Behavioral knobs</h3>
    <table class="grid">
      <thead>
        <tr><th>Knob</th><th>Current</th><th>Range</th><th>Step limit</th><th>Cooldown</th></tr>
      </thead>
      <tbody>
        <tr v-for="(k, name) in tpl.behavioral_knobs" :key="String(name)">
          <td>{{ name }}</td>
          <td>{{ k.current }}</td>
          <td>{{ k.min }} – {{ k.max }}</td>
          <td>{{ k.step_limit }}</td>
          <td>{{ k.cooldown_hours }}h</td>
        </tr>
      </tbody>
    </table>

    <h3>Evolution rules</h3>
    <table class="grid" v-if="(tpl.evolution_rules || []).length">
      <thead>
        <tr><th>Rule</th><th>Event</th><th>Target</th><th>Action</th><th>Amount</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in tpl.evolution_rules" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.event }}</td>
          <td>{{ r.target }}</td>
          <td>{{ r.action }}</td>
          <td>{{ r.amount }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else>（no evolution rules）</p>

    <p>
      <button class="toggle" @click="showRaw = !showRaw">
        {{ showRaw ? "Hide raw YAML" : "Show raw YAML source" }}
      </button>
    </p>
    <pre v-if="showRaw" class="raw">{{ raw }}</pre>
  </div>
</template>

<style scoped>
.grid { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }
.grid th, .grid td { padding: .35rem .6rem; border-bottom: 1px solid #eee; text-align: left; }
.raw { background: #fafafa; padding: 1rem; border: 1px solid #ddd; border-radius: .3rem; overflow: auto; }
.toggle { font-size: 0.9rem; }
.err { color: crimson; }
</style>
