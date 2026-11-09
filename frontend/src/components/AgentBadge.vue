<template>
  <span class="agent-badge" :data-agent="normalized">{{ agentLabel(agent) }}</span>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { agentLabel } from "../constants/agents";

const props = defineProps<{ agent: string }>();
const normalized = computed(() => props.agent.trim().toLowerCase() || "generic");
</script>

<style scoped>
.agent-badge {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  padding: 1px 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 42%, var(--border));
  border-radius: 999px;
  background: var(--accent-weak);
  color: var(--accent-strong);
  font-size: 11px;
  font-weight: 600;
  line-height: 1.5;
  text-transform: none;
  white-space: nowrap;
}

.agent-badge[data-agent="claude"] {
  border-color: color-mix(in srgb, #d99a6c 50%, var(--border));
  background: color-mix(in srgb, #d99a6c 13%, transparent);
  color: #e9ad80;
}

.agent-badge[data-agent="stepcode"] {
  border-color: color-mix(in srgb, #79c7a3 48%, var(--border));
  background: color-mix(in srgb, #79c7a3 12%, transparent);
  color: #8bd9b5;
}

.agent-badge[data-agent="stepcli"] {
  border-color: color-mix(in srgb, #7db4ef 48%, var(--border));
  background: color-mix(in srgb, #7db4ef 12%, transparent);
  color: #9bc8f5;
}

.agent-badge[data-agent="dsh"] {
  border-color: color-mix(in srgb, #c59af0 48%, var(--border));
  background: color-mix(in srgb, #c59af0 12%, transparent);
  color: #cda9f1;
}

.agent-badge[data-agent="opencode"] {
  border-color: color-mix(in srgb, #e7b75f 48%, var(--border));
  background: color-mix(in srgb, #e7b75f 12%, transparent);
  color: #edc777;
}

.agent-badge[data-agent="pi"] {
  border-color: color-mix(in srgb, #6fc9d8 48%, var(--border));
  background: color-mix(in srgb, #6fc9d8 12%, transparent);
  color: #8ddce8;
}

.agent-badge[data-agent="xdcode"] {
  border-color: color-mix(in srgb, #9aa8f2 48%, var(--border));
  background: color-mix(in srgb, #9aa8f2 12%, transparent);
  color: #b2bdf7;
}

.agent-badge[data-agent="stepgo"] {
  border-color: color-mix(in srgb, #f08bb0 48%, var(--border));
  background: color-mix(in srgb, #f08bb0 12%, transparent);
  color: #f5a4c2;
}

.agent-badge[data-agent="generic"] {
  border-color: var(--border);
  background: var(--panel-2);
  color: var(--fg-muted);
}
</style>
