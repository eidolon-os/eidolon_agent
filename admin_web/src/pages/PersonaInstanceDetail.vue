<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "../api/client";

const route = useRoute();
const router = useRouter();
const tenant = String(route.params.tenant);
const user = String(route.params.user);
const instanceId = String(route.params.instance);

const snapshot = ref<any | null>(null);
const history = ref<any[]>([]);
const err = ref<string>("");
const busy = ref(false);

const basePath = computed(
  () => `/admin/personas/instances/${encodeURIComponent(tenant)}/${encodeURIComponent(user)}/${encodeURIComponent(instanceId)}`,
);

async function refresh() {
  try {
    snapshot.value = (await client().get(`${basePath.value}/snapshot`)).data;
    history.value = (await client().get(`${basePath.value}/evolution`)).data;
  } catch (e: any) {
    err.value = e.message;
  }
}

async function rollback(deltaId: string) {
  if (!confirm(`Rollback delta ${deltaId}? Bumps overlay_version and writes a new audit row.`)) return;
  busy.value = true;
  try {
    await client().post(`${basePath.value}/rollback`, { delta_id: deltaId });
    await refresh();
  } catch (e: any) {
    err.value = e.message;
  } finally {
    busy.value = false;
  }
}

async function deleteInstance() {
  if (!confirm("Delete this instance? This cannot be undone — the user's overlay is lost.")) return;
  busy.value = true;
  try {
    await client().delete(basePath.value);
    router.push("/instances");
  } catch (e: any) {
    err.value = e.message;
  } finally {
    busy.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <p><router-link to="/instances">← Back to instances</router-link></p>
  <p v-if="err" class="err">{{ err }}</p>

  <section v-if="snapshot">
    <h2>{{ snapshot.instance.metadata.name }}</h2>
    <p>
      <code>{{ snapshot.instance.instance_id }}</code>
      · tenant <code>{{ snapshot.instance.tenant_id }}</code>
      · user <code>{{ snapshot.instance.user_id }}</code>
      · overlay <code>v{{ snapshot.instance.overlay_version }}</code>
    </p>

    <h3>Current state</h3>
    <table class="grid">
      <tr>
        <th>Mood (dominant)</th>
        <td>{{ snapshot.runtime_state?.mood?.intensity ? "intensity=" + snapshot.runtime_state.mood.intensity : "（no mood）" }}</td>
      </tr>
      <tr>
        <th>Energy</th>
        <td>{{ snapshot.runtime_state?.energy?.level ?? "—" }}</td>
      </tr>
      <tr>
        <th>Attention</th>
        <td>{{ snapshot.runtime_state?.attention?.target ?? "—" }}</td>
      </tr>
      <tr>
        <th>Prompt hint</th>
        <td>{{ snapshot.prompt_hint || "（empty）" }}</td>
      </tr>
    </table>

    <h3>Evolution history</h3>
    <table class="grid" v-if="history.length">
      <thead>
        <tr>
          <th>Rationale</th>
          <th>Changes</th>
          <th>Applied</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(r, idx) in history" :key="idx">
          <td>{{ r.rationale }}</td>
          <td>
            <code v-for="c in (r.changes || [])" :key="c.path">
              {{ c.path }}: {{ c.old }} → {{ c.new }}<br>
            </code>
          </td>
          <td>{{ r.applied ? "✓" : "—" }}</td>
          <td>
            <button :disabled="busy || !r.applied" @click="rollback((r as any).delta_id || '')">
              Rollback
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="empty">（no evolution recorded yet）</p>

    <h3 class="danger-heading">Danger zone</h3>
    <button class="danger" :disabled="busy" @click="deleteInstance">
      Delete this instance
    </button>
  </section>
</template>

<style scoped>
.grid { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }
.grid th, .grid td { padding: .4rem .7rem; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }
.empty { color: #888; padding: 1rem; }
.err { color: crimson; }
.danger { background: #c81e1e; color: white; border: none; padding: .5rem 1rem; border-radius: .3rem; cursor: pointer; }
.danger:disabled { opacity: .5; cursor: not-allowed; }
.danger-heading { color: #c81e1e; margin-top: 2rem; }
</style>
