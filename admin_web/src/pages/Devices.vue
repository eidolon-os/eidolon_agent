<script setup lang="ts">
import { ref } from "vue";
import { client } from "../api/client";

const tenantId = ref("demo");
const userId = ref("alice");
const template = ref("caretaker_jiezhi");
const code = ref<string>("");
const qrUrl = ref<string>("");
const err = ref<string>("");

async function issue() {
  err.value = "";
  try {
    const { data } = await client().post("/admin/pairing/codes", {
      tenant_id: tenantId.value,
      user_id: userId.value,
      default_template_id: template.value,
    });
    code.value = data.code;
    qrUrl.value = `/api/admin/pairing/codes/${data.code}.png`;
  } catch (e: any) {
    err.value = e.response?.data?.detail ?? e.message;
  }
}
</script>

<template>
  <h2>Devices &amp; pairing</h2>
  <form @submit.prevent="issue">
    <label>tenant: <input v-model="tenantId" /></label>
    <label>user: <input v-model="userId" /></label>
    <label>template: <input v-model="template" /></label>
    <button>Issue pairing code</button>
  </form>
  <p v-if="err" class="err">{{ err }}</p>
  <div v-if="code">
    <p>Code: <code>{{ code }}</code></p>
    <img :src="qrUrl" alt="pairing QR" />
  </div>
</template>

<style scoped>
form label { margin-right: 1rem; }
.err { color: crimson; }
img { max-width: 240px; }
</style>
