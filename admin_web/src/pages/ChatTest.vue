<script setup lang="ts">
import { ref } from "vue";
import { client } from "../api/client";

const form = ref({
  tenant_id: "demo",
  user_id: "alice",
  template_id: "caretaker_jiezhi",
  text: "",
});

interface ChatResult {
  turn_id: string;
  assistant_text: string;
  triage: string;
  latency_first_delta_ms: number;
  user_id: string;
}

const result = ref<ChatResult | null>(null);
const error = ref("");
const sending = ref(false);
const history = ref<{ role: string; text: string }[]>([]);

async function send() {
  if (!form.value.text.trim() || sending.value) return;
  const text = form.value.text.trim();
  form.value.text = "";
  error.value = "";
  sending.value = true;
  history.value.push({ role: "user", text });

  try {
    const resp = await client().post("/admin/chat/test", {
      ...form.value,
      text,
    });
    result.value = resp.data;
    history.value.push({ role: "assistant", text: resp.data.assistant_text });
  } catch (e: any) {
    error.value = e.response?.data?.detail ?? e.message;
    history.value.push({ role: "error", text: error.value });
  } finally {
    sending.value = false;
  }
}
</script>

<template>
  <h2>Chat Test (gRPC)</h2>
  <p class="desc">
    Full path: Admin HTTP → gRPC pairing → gRPC ChatOnce → TurnEngine → LLM
  </p>

  <div class="config">
    <label>template: <input v-model="form.template_id" /></label>
    <label>tenant: <input v-model="form.tenant_id" /></label>
    <label>user: <input v-model="form.user_id" /></label>
  </div>

  <div class="chat-box">
    <div class="messages">
      <div v-for="(msg, i) in history" :key="i" :class="['msg', msg.role]">
        <span class="role">{{ msg.role }}</span>
        <span class="text">{{ msg.text }}</span>
      </div>
      <div v-if="sending" class="msg assistant loading">
        <span class="role">assistant</span>
        <span class="text">thinking...</span>
      </div>
      <div v-if="!history.length && !sending" class="empty">
        Send a message to test the full gRPC pipeline.
      </div>
    </div>
  </div>

  <div class="chat-input">
    <input
      v-model="form.text"
      placeholder="Type a message..."
      @keydown.enter="send"
      :disabled="sending"
    />
    <button @click="send" :disabled="sending || !form.text.trim()">
      {{ sending ? "..." : "Send" }}
    </button>
  </div>

  <div v-if="result" class="meta">
    <span>triage: <code>{{ result.triage }}</code></span>
    <span>first_delta: <code>{{ result.latency_first_delta_ms }}ms</code></span>
    <span>turn: <code>{{ result.turn_id.slice(0, 8) }}</code></span>
  </div>
</template>

<style scoped>
.desc { color: #888; font-size: .85rem; margin-top: 0; }
.config { display: flex; gap: 1rem; margin-bottom: 1rem; }
.config label { font-size: .9rem; }
.config input { width: 140px; padding: .3rem .4rem; }

.chat-box {
  border: 1px solid #e0e0e0; border-radius: 8px;
  padding: .8rem; margin-bottom: .8rem;
  max-height: 400px; overflow-y: auto; background: #fafafa;
}
.messages { display: flex; flex-direction: column; gap: .5rem; }
.msg { display: flex; gap: .5rem; align-items: baseline; }
.msg .role {
  font-size: .75rem; font-weight: 600; text-transform: uppercase;
  min-width: 70px; text-align: right; color: #888;
}
.msg .text { white-space: pre-wrap; line-height: 1.5; }
.msg.user .role { color: #2563eb; }
.msg.assistant .role { color: #16a34a; }
.msg.error .role { color: #dc2626; }
.msg.error .text { color: #dc2626; }
.msg.loading .text { color: #999; font-style: italic; }
.empty { color: #aaa; font-style: italic; text-align: center; padding: 2rem; }

.chat-input { display: flex; gap: .5rem; margin-bottom: .5rem; }
.chat-input input { flex: 1; padding: .5rem; font-size: 1rem; }
.chat-input button { padding: .5rem 1.2rem; }

.meta { display: flex; gap: 1.5rem; font-size: .8rem; color: #888; }
.meta code { background: #f0f0f0; padding: .1rem .3rem; border-radius: 3px; }
</style>
