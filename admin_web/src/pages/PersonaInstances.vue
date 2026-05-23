<script setup lang="ts">
import { onMounted, ref } from "vue";
import { client } from "../api/client";

interface InstanceRow {
  instance_id: string;
  tenant_id: string;
  user_id: string;
  template_id: string;
  overlay_version: number;
  created_at: string;
  updated_at: string;
}

const rows = ref<InstanceRow[]>([]);
const err = ref<string>("");

onMounted(async () => {
  try {
    rows.value = (await client().get("/admin/personas/instances")).data;
  } catch (e: any) {
    err.value = e.message;
  }
});

function detailHref(r: InstanceRow): string {
  return `/instances/${encodeURIComponent(r.tenant_id)}/${encodeURIComponent(r.user_id)}/${encodeURIComponent(r.instance_id)}`;
}
</script>

<template>
  <h2>Persona instances</h2>
  <p v-if="err" class="err">{{ err }}</p>
  <table class="grid">
    <thead>
      <tr>
        <th>Tenant</th>
        <th>User</th>
        <th>Instance</th>
        <th>Template</th>
        <th>Overlay v</th>
        <th>Updated</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="r in rows" :key="r.instance_id">
        <td>{{ r.tenant_id }}</td>
        <td>{{ r.user_id }}</td>
        <td><code>{{ r.instance_id }}</code></td>
        <td>{{ r.template_id }}</td>
        <td><code>v{{ r.overlay_version }}</code></td>
        <td><time>{{ r.updated_at }}</time></td>
        <td><router-link :to="detailHref(r)">Details →</router-link></td>
      </tr>
      <tr v-if="!rows.length">
        <td colspan="7" class="empty">（no instances yet — first turn lazily creates one）</td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.grid { width: 100%; border-collapse: collapse; }
.grid th, .grid td { padding: .5rem .75rem; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }
.empty { color: #888; text-align: center; padding: 2rem; }
.err { color: crimson; }
</style>
