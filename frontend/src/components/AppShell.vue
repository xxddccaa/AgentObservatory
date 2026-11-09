<template>
  <div class="shell">
    <aside class="shell__rail">
      <router-link to="/" class="shell__brand" title="Agent Observatory">
        <span class="shell__logo"><Telescope :size="18" /></span>
        <span class="shell__brandcopy">
          <strong class="shell__brandname">Agent Observatory</strong>
          <span class="shell__brandmeta">Multi-agent sessions</span>
        </span>
      </router-link>

      <nav class="shell__nav" aria-label="Primary">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="navlink"
          :class="{ 'navlink--active': isActive(item) }"
        >
          <component :is="item.icon" :size="16" />
          <span class="navlink__label">{{ item.label }}</span>
          <span v-if="isActive(item)" class="navlink__marker" aria-hidden="true" />
        </router-link>
      </nav>

      <div class="shell__rail-footer">
        <span class="shell__env" :data-online="online" :title="envTitle">
          <span class="shell__dot" />
          <span class="shell__env-copy">
            <span class="shell__env-label">{{ envLabel }}</span>
            <small>{{ online ? "Services operational" : "Attention required" }}</small>
          </span>
        </span>

        <UserMenu />
      </div>
    </aside>

    <div class="shell__content">
      <aside v-if="healthProblem" class="health-banner" role="status">
        <div>
          <strong>{{ healthProblem.title }}</strong>
          <span>{{ healthProblem.detail }}</span>
        </div>
        <router-link v-if="auth.isAdmin" to="/system">Open System</router-link>
        <button v-else type="button" @click="tick">Retry</button>
      </aside>

      <main class="shell__body">
        <slot />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  ChartNoAxesCombined,
  BookOpen,
  Gauge,
  History,
  BrainCircuit,
  FolderKanban,
  HardDrive,
  ServerCog,
  ShieldCheck,
  Telescope,
} from "@lucide/vue";
import { useAuthStore } from "../stores/auth";
import { getReadyHealth, type ReadyHealth } from "../api/health";
import UserMenu from "./UserMenu.vue";

const auth = useAuthStore();
const route = useRoute();

interface NavItem {
  to: string;
  label: string;
  icon: unknown;
  match: string;
}

const navItems = computed<NavItem[]>(() => {
  const items: NavItem[] = [
    { to: "/", label: "Dashboard", icon: Gauge, match: "dashboard" },
    { to: "/history", label: "History", icon: History, match: "history" },
    { to: "/memories", label: "Memories", icon: BrainCircuit, match: "memories" },
    { to: "/projects", label: "Projects", icon: FolderKanban, match: "projects" },
    { to: "/insights", label: "Insights", icon: ChartNoAxesCombined, match: "insights" },
    { to: "/guide", label: "使用方法", icon: BookOpen, match: "guide" },
    { to: "/audit", label: "Audit", icon: ShieldCheck, match: "audit" },
  ];
  if (auth.canOperate) {
    items.push({ to: "/storage", label: "Storage", icon: HardDrive, match: "storage" });
  }
  if (auth.isAdmin) {
    items.push({ to: "/system", label: "System", icon: ServerCog, match: "system" });
  }
  return items;
});

function isActive(item: NavItem): boolean {
  const name = String(route.name ?? "");
  if (item.match === "dashboard") return name === "dashboard";
  if (item.match === "history") return name === "history" || name === "history-detail";
  if (item.match === "projects") return name === "projects" || name === "project-detail";
  return name === item.match;
}

const online = ref(false);
const envLabel = ref("connecting…");
const envTitle = ref("Checking API health…");
const readiness = ref<ReadyHealth | null>(null);
const apiOffline = ref(false);
let timer: ReturnType<typeof setInterval> | null = null;

const healthProblem = computed(() => {
  if (apiOffline.value) {
    return {
      title: "Observatory API is offline.",
      detail: "Start it with `agent-observatory up`, then retry.",
    };
  }
  if (readiness.value?.status !== "degraded") return null;
  const unhealthy = Object.entries(readiness.value.components)
    .filter(([, component]) => component.status !== "up")
    .map(([name, component]) => `${name} ${component.status}`)
    .join(", ");
  const recovery = readiness.value.recovery_actions[0];
  return {
    title: "Observatory is degraded.",
    detail: unhealthy
      ? `${unhealthy}.${recovery ? ` ${recovery}` : ""}`
      : recovery || "One or more background components need attention.",
  };
});

async function tick() {
  try {
    const health = await getReadyHealth();
    readiness.value = health;
    apiOffline.value = false;
    online.value = health.status === "ok";
    envLabel.value = health.environment;
    envTitle.value = Object.entries(health.components)
      .map(([name, component]) => `${name} ${component.status}`)
      .join(" · ");
  } catch {
    readiness.value = null;
    apiOffline.value = true;
    online.value = false;
    envLabel.value = "API offline";
    envTitle.value = "API unreachable";
  }
}

onMounted(() => {
  void tick();
  timer = setInterval(tick, 10_000);
});
onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
});
</script>

<style scoped>
.shell {
  min-height: 100dvh;
  display: grid;
  grid-template-columns: 224px minmax(0, 1fr);
  background: var(--bg);
  color: var(--fg);
}
.shell__rail {
  position: fixed;
  inset: 0 auto 0 0;
  z-index: 30;
  display: flex;
  width: 224px;
  min-height: 100dvh;
  flex-direction: column;
  align-items: center;
  padding: 18px 12px 12px;
  border-right: 1px solid var(--border);
  background: #121412;
}
.shell__brand {
  display: flex;
  align-items: center;
  width: 100%;
  gap: 10px;
  padding: 0 7px 18px;
  color: var(--fg);
  text-decoration: none;
  white-space: nowrap;
}
.shell__logo {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  border: 1px solid rgba(139, 179, 255, 0.24);
  border-radius: 7px;
  background: var(--accent-weak);
  color: var(--accent-strong);
}
.shell__brandcopy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  line-height: 1.25;
}
.shell__brandname {
  overflow: hidden;
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
}
.shell__brandmeta {
  color: var(--fg-subtle);
  font-size: 10px;
  text-transform: uppercase;
}
.shell__nav {
  display: flex;
  width: 100%;
  flex-direction: column;
  gap: 3px;
  margin-top: 15px;
  min-width: 0;
}
.navlink {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 38px;
  padding: 0 11px;
  border-radius: 6px;
  color: var(--fg-muted);
  text-decoration: none;
  white-space: nowrap;
  transition: background-color 120ms ease, color 120ms ease;
}
.navlink:hover {
  background: var(--panel-2);
  color: var(--fg);
}
.navlink--active {
  background: var(--accent-weak);
  color: var(--accent-strong);
}
.navlink__label {
  font-size: 13px;
  font-weight: 520;
}
.navlink__marker {
  position: absolute;
  right: 7px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
}
.shell__rail-footer {
  display: grid;
  width: 100%;
  gap: 8px;
  margin-top: auto;
}
.shell__new {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  gap: 8px;
  height: 38px;
  padding: 0 12px;
  border: 1px solid rgba(139, 179, 255, 0.24);
  border-radius: 6px;
  background: var(--accent-weak);
  color: var(--accent-strong);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: border-color 120ms ease, background-color 120ms ease;
}
.shell__new:hover {
  border-color: rgba(139, 179, 255, 0.42);
  background: rgba(110, 159, 248, 0.18);
}
.shell__env {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
  padding: 9px 10px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  color: var(--fg-muted);
}
.shell__dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--danger);
  box-shadow: 0 0 0 3px var(--danger-weak);
}
.shell__env[data-online="true"] .shell__dot {
  background: var(--ok);
  box-shadow: 0 0 0 3px var(--ok-weak);
}
.shell__env-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  line-height: 1.3;
}
.shell__env-label {
  overflow: hidden;
  color: var(--fg);
  font-size: 11px;
  font-weight: 600;
  text-overflow: ellipsis;
  text-transform: uppercase;
  white-space: nowrap;
}
.shell__env-copy small {
  overflow: hidden;
  color: var(--fg-subtle);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.shell__content {
  grid-column: 2;
  min-width: 0;
  min-height: 100dvh;
}
.shell__body {
  min-height: 0;
}
.health-banner {
  position: sticky;
  top: 0;
  z-index: 25;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 18px;
  min-height: 42px;
  padding: 8px 18px;
  border-bottom: 1px solid rgba(231, 183, 95, 0.35);
  background: #292418;
  color: #f2ce88;
  font-size: 13px;
}
.health-banner div {
  display: flex;
  gap: 7px;
}
.health-banner a,
.health-banner button {
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-weight: 700;
  text-decoration: underline;
  cursor: pointer;
  white-space: nowrap;
}
@media (max-width: 900px) {
  .shell {
    display: block;
    padding-bottom: 58px;
  }
  .shell__rail {
    position: sticky;
    top: 0;
    width: 100%;
    min-height: 0;
    height: 54px;
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr) auto auto;
    gap: 8px;
    padding: 0 10px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .shell__brand {
    width: auto;
    padding: 0;
  }
  .shell__logo {
    width: 32px;
    height: 32px;
  }
  .shell__brandcopy {
    display: none;
  }
  .shell__new {
    width: 34px;
    height: 34px;
    padding: 0;
  }
  .shell__nav {
    position: fixed;
    inset: auto 0 0;
    z-index: 35;
    display: flex;
    width: 100%;
    height: 58px;
    flex-direction: row;
    align-items: stretch;
    justify-content: space-around;
    gap: 0;
    margin: 0;
    padding: 5px max(6px, env(safe-area-inset-right)) max(5px, env(safe-area-inset-bottom)) max(6px, env(safe-area-inset-left));
    border-top: 1px solid var(--border);
    background: #121412;
  }
  .navlink {
    width: min(52px, 15vw);
    height: 47px;
    justify-content: center;
    padding: 0;
  }
  .navlink__marker {
    right: auto;
    bottom: 3px;
    width: 14px;
    height: 2px;
    border-radius: 2px;
  }
  .shell__rail-footer {
    display: contents;
  }
  .shell__env {
    grid-column: 4;
    width: 34px;
    height: 34px;
    justify-content: center;
    padding: 0;
    border: 0;
  }
  .shell__env-copy {
    display: none;
  }
  .shell__rail-footer :deep(.usermenu) {
    grid-column: 5;
  }
  .shell__content {
    min-height: calc(100dvh - 112px);
  }
  .health-banner {
    top: 54px;
  }
  .shell__brandname,
  .shell__brandmeta,
  .shell__new-label,
  .shell__env-label,
  .navlink__label {
    display: none;
  }
  .health-banner {
    align-items: flex-start;
    justify-content: space-between;
  }
  .health-banner div {
    flex-direction: column;
    gap: 0;
  }
}
</style>
