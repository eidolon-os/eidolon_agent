<script setup lang="ts">
import { onMounted, ref } from "vue";
import { client } from "../api/client";

const instances = ref<any[]>([]);
const err = ref<string>("");

const newInst = ref({ template_id: "caretaker_jiezhi", tenant_id: "demo", user_id: "alice" });

async function reload() {
  try {
    instances.value = (await client().get("/admin/agents")).data;
  } catch (e: any) {
    err.value = e.message;
  }
}

async function start() {
  try {
    await client().post("/admin/agents", newInst.value);
    await reload();
  } catch (e: any) {
    err.value = e.response?.data?.detail ?? e.message;
  }
}

async function stop(id: string) {
  await client().delete(`/admin/agents/${id}`);
  await reload();
}

onMounted(reload);
</script>

<template>
  <h2>Agent instances</h2>
  <form @submit.prevent="start">
    <label>template: <input v-model="newInst.template_id" /></label>
    <label>tenant: <input v-model="newInst.tenant_id" /></label>
    <label>user: <input v-model="newInst.user_id" /></label>
    <button>Start</button>
  </form>
  <p v-if="err" class="err">{{ err }}</p>
  <table>
    <thead>
      <tr><th>ID</th><th>Template</th><th>Tenant/User</th><th>Status</th><th></th></tr>
    </thead>
    <tbody>
      <tr v-for="i in instances" :key="i.instance_id">
        <td><code>{{ i.instance_id }}</code></td>
        <td>{{ i.template_id }}</td>
        <td>{{ i.tenant_id }} / {{ i.user_id }}</td>
        <td>{{ i.status }}</td>
        <td><button @click="stop(i.instance_id)">Stop</button></td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
form label { margin-right: 1rem; }
table { border-collapse: collapse; margin-top: 1rem; }
th, td { border: 1px solid #ddd; padding: .4rem .7rem; text-align: left; }
.err { color: crimson; }
</style>
