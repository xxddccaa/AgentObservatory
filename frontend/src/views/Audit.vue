<template>
  <div class="list-page">
    <header class="page-header">
      <div><p class="eyebrow">Immutable activity trail</p><h1>Audit</h1></div>
      <button class="refresh" title="Refresh audit log" @click="load"><RefreshCw :size="16" />Refresh</button>
    </header>
    <div class="filters">
      <input v-model="action" placeholder="Filter action" aria-label="Filter action" @keydown.enter="load" />
      <button class="refresh" @click="load">Apply</button>
    </div>
    <div v-if="error" class="empty error">{{ error }}</div>
    <div v-else class="audit-list">
      <article v-for="item in items" :key="item.id" class="audit-row">
        <div class="audit-icon" :data-result="item.result"><ShieldCheck :size="16" /></div>
        <div class="audit-copy"><strong>{{ item.action }}</strong><span>{{ item.resource_type || "system" }} · {{ item.actor_id || item.actor_type }}</span></div>
        <time>{{ formatDate(item.created_at) }}</time>
        <span class="result" :data-result="item.result">{{ item.result }}</span>
      </article>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { RefreshCw, ShieldCheck } from "@lucide/vue";
const action = ref("");
const items = ref<Array<{ id: number; action: string; resource_type: string | null; actor_id: string | null; actor_type: string; result: string; created_at: string }>>([]);
const error = ref("");
async function load() {
  const query = action.value ? `?action=${encodeURIComponent(action.value)}` : "";
  try {
    const response = await fetch(`/api/audit${query}`, { credentials: "include" });
    if (!response.ok) throw new Error(`Audit request failed (${response.status})`);
    items.value = (await response.json()).items;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : "Unable to load audit"; }
}
function formatDate(value: string) { return new Date(value).toLocaleString(); }
onMounted(load);
</script>

<style scoped>
.list-page { max-width: 1120px; margin: 0 auto; padding: 30px 22px; }
.page-header { display: flex; align-items: end; justify-content: space-between; margin-bottom: 20px; }
.eyebrow { margin: 0 0 5px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
h1 { margin: 0; font-size: 25px; letter-spacing: 0; }
.refresh { display: flex; align-items: center; gap: 7px; height: 34px; padding: 0 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); cursor: pointer; }
.filters { display: flex; gap: 8px; margin-bottom: 12px; }
.filters input { flex: 1; min-width: 0; height: 34px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); color: var(--fg); padding: 0 10px; }
.audit-list { display: grid; gap: 5px; }
.audit-row { display: grid; grid-template-columns: 28px 1fr auto auto; align-items: center; gap: 10px; padding: 11px 12px; border: 1px solid var(--border); border-radius: 5px; background: var(--panel); }
.audit-icon { color: var(--ok); }
.audit-icon[data-result="failure"], .result[data-result="failure"] { color: var(--danger); }
.audit-copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.audit-copy span, time { color: var(--fg-muted); font-size: 12px; }
.result { color: var(--ok); font-size: 12px; }
.empty { padding: 56px 20px; border: 1px dashed var(--border); color: var(--fg-muted); text-align: center; }
.error { color: var(--danger); }
@media (max-width: 700px) { .audit-row { grid-template-columns: 28px 1fr auto; } time { grid-column: 2; } .result { grid-column: 3; grid-row: 1 / span 2; } }
</style>
