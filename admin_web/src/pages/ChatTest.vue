<script setup lang="ts">
import { nextTick, ref } from "vue";

const form = ref({
  tenant_id: "demo",
  user_id: "alice",
  template_id: "caretaker_jiezhi",
  text: "",
});

interface Msg {
  role: "user" | "assistant" | "error" | "status";
  text: string;
  meta?: Record<string, unknown>;
}

const history = ref<Msg[]>([]);
const sending = ref(false);
const boxEl = ref<HTMLElement | null>(null);

function scrollToBottom() {
  nextTick(() => {
    if (boxEl.value) boxEl.value.scrollTop = boxEl.value.scrollHeight;
  });
}

function parseSSE(buffer: string): { events: { type: string; data: any }[]; remaining: string } {
  const events: { type: string; data: any }[] = [];
  const parts = buffer.split("\n\n");
  const remaining = parts.pop() || "";
  for (const part of parts) {
    let type = "";
    let dataStr = "";
    for (const line of part.split("\n")) {
      if (line.startsWith("event: ")) type = line.slice(7);
      else if (line.startsWith("data: ")) dataStr = line.slice(6);
    }
    if (dataStr) {
      try {
        events.push({ type, data: JSON.parse(dataStr) });
      } catch (e) {
        events.push({ type: "error", data: { message: "parse error: " + dataStr } });
      }
    }
  }
  return { events, remaining };
}

async function send() {
  if (!form.value.text.trim() || sending.value) return;
  const text = form.value.text.trim();
  form.value.text = "";
  sending.value = true;

  history.value.push({ role: "user", text });
  history.value.push({ role: "assistant", text: "", meta: {} });
  const idx = history.value.length - 1;
  const assistant = () => history.value[idx];  // always read through the reactive proxy
  scrollToBottom();

  try {
    const resp = await fetch("/api/admin/chat/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...form.value, text }),
    });
    if (!resp.ok || !resp.body) {
      assistant().role = "error";
      assistant().text = `HTTP ${resp.status}`;
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { events, remaining } = parseSSE(buffer);
      buffer = remaining;

      for (const ev of events) {
        if (ev.type === "status") continue;
        const kind = ev.data.kind;
        const data = ev.data.data || {};
        if (kind === "DELTA" && typeof data.text === "string") {
          assistant().text += data.text;
          scrollToBottom();
        } else if (kind === "DONE") {
          assistant().meta = data;
        } else if (kind === "ERROR") {
          assistant().role = "error";
          assistant().text = data.message || JSON.stringify(data);
        }
      }
    }
  } catch (e: unknown) {
    assistant().role = "error";
    assistant().text = e instanceof Error ? e.message : String(e);
  } finally {
    sending.value = false;
    scrollToBottom();
  }
}
</script>

<template>
  <h2>Chat Test (gRPC)</h2>
  <p class="desc">Full path: Admin SSE → gRPC pairing → gRPC Chat bidi → TurnEngine → LLM stream</p>

  <div class="config">
    <label>template: <input v-model="form.template_id" /></label>
    <label>tenant: <input v-model="form.tenant_id" /></label>
    <label>user: <input v-model="form.user_id" /></label>
  </div>

  <div class="chat-box" ref="boxEl">
    <div v-for="(msg, i) in history" :key="i" :class="['msg', msg.role]">
      <span class="role">{{ msg.role }}</span>
      <span class="text">{{ msg.text }}<span v-if="sending && i === history.length - 1 && msg.role === 'assistant'" class="cursor">▋</span></span>
    </div>
    <div v-if="!history.length" class="empty">Send a message to test the gRPC streaming pipeline.</div>
  </div>

  <div class="chat-input">
    <input
      v-model="form.text"
      placeholder="Type a message..."
      @keydown.enter="send"
      :disabled="sending"
    />
    <button @click="send" :disabled="sending || !form.text.trim()">{{ sending ? "..." : "Send" }}</button>
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
  max-height: 500px; overflow-y: auto; background: #fafafa;
  display: flex; flex-direction: column; gap: .5rem;
}
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
.cursor { animation: blink 1s steps(2, start) infinite; color: #16a34a; }
@keyframes blink { to { visibility: hidden; } }
.empty { color: #aaa; font-style: italic; text-align: center; padding: 2rem; }

.chat-input { display: flex; gap: .5rem; }
.chat-input input { flex: 1; padding: .5rem; font-size: 1rem; }
.chat-input button { padding: .5rem 1.2rem; }
</style>
