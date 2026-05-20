<script setup lang="ts">
import { ref, nextTick } from "vue";

const form = ref({
  tenant_id: "demo",
  user_id: "alice",
  template_id: "caretaker_jiezhi",
  text: "",
});

interface ChatEvent {
  kind: string;
  seq?: number;
  data?: Record<string, unknown>;
  message?: string;
}

const events = ref<ChatEvent[]>([]);
const assistantText = ref("");
const sending = ref(false);
const logEl = ref<HTMLElement | null>(null);

function scrollToBottom() {
  nextTick(() => {
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
  });
}

async function send() {
  if (!form.value.text.trim() || sending.value) return;
  events.value = [];
  assistantText.value = "";
  sending.value = true;

  try {
    const resp = await fetch("/api/admin/chat/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form.value),
    });

    if (!resp.ok || !resp.body) {
      events.value.push({ kind: "ERROR", message: `HTTP ${resp.status}` });
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";

      for (const part of parts) {
        const lines = part.split("\n");
        let eventType = "";
        let dataStr = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventType = line.slice(7);
          else if (line.startsWith("data: ")) dataStr = line.slice(6);
        }
        if (!dataStr) continue;

        const parsed = JSON.parse(dataStr);

        if (eventType === "status") {
          events.value.push({ kind: "STATUS", message: parsed.message });
        } else if (eventType === "event") {
          events.value.push(parsed);
          if (parsed.kind === "DELTA" && parsed.data?.text) {
            assistantText.value += parsed.data.text;
          }
        }
        scrollToBottom();
      }
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    events.value.push({ kind: "ERROR", message: msg });
  } finally {
    sending.value = false;
  }
}
</script>

<template>
  <h2>Chat Test (gRPC)</h2>
  <p class="desc">
    Full path: Admin HTTP → gRPC pairing → gRPC Chat bidi stream
  </p>

  <div class="config">
    <label>template: <input v-model="form.template_id" /></label>
    <label>tenant: <input v-model="form.tenant_id" /></label>
    <label>user: <input v-model="form.user_id" /></label>
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

  <div v-if="assistantText" class="reply">
    <strong>Reply:</strong>
    <div class="reply-text">{{ assistantText }}</div>
  </div>

  <div class="event-log" ref="logEl">
    <div v-for="(ev, i) in events" :key="i" :class="['ev', ev.kind.toLowerCase()]">
      <span class="kind">{{ ev.kind }}</span>
      <span v-if="ev.seq != null" class="seq">#{{ ev.seq }}</span>
      <span class="body">{{ ev.message || JSON.stringify(ev.data) }}</span>
    </div>
    <div v-if="!events.length" class="empty">Events will appear here...</div>
  </div>
</template>

<style scoped>
.desc { color: #888; font-size: .85rem; margin-top: 0; }
.config { display: flex; gap: 1rem; margin-bottom: 1rem; }
.config label { font-size: .9rem; }
.config input { width: 140px; padding: .3rem .4rem; }
.chat-input { display: flex; gap: .5rem; margin-bottom: 1rem; }
.chat-input input { flex: 1; padding: .5rem; font-size: 1rem; }
.chat-input button { padding: .5rem 1.2rem; }
.reply { background: #f0f9f0; border: 1px solid #c3e6c3; border-radius: 6px; padding: .8rem; margin-bottom: 1rem; }
.reply-text { margin-top: .4rem; white-space: pre-wrap; }
.event-log {
  background: #1e1e1e; color: #d4d4d4; border-radius: 6px;
  padding: .8rem; font-family: monospace; font-size: .82rem;
  max-height: 400px; overflow-y: auto;
}
.ev { padding: .15rem 0; }
.kind { display: inline-block; width: 80px; font-weight: bold; }
.seq { color: #888; margin-right: .5rem; }
.ev.delta .kind { color: #6a9955; }
.ev.done .kind { color: #569cd6; }
.ev.error .kind { color: #f44747; }
.ev.state .kind { color: #dcdcaa; }
.ev.status .kind { color: #ce9178; }
.ev.tool_call .kind, .ev.tool_result .kind { color: #c586c0; }
.empty { color: #666; font-style: italic; }
</style>
