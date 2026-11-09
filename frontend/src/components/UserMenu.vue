<template>
  <div ref="root" class="usermenu">
    <button
      class="usermenu__trigger"
      :aria-expanded="open"
      aria-haspopup="menu"
      @click="open = !open"
    >
      <span class="usermenu__avatar">{{ initial }}</span>
      <span class="usermenu__name">{{ auth.username }}</span>
      <ChevronDown :size="15" />
    </button>

    <div v-if="open" class="usermenu__panel" role="menu">
      <div class="usermenu__head">
        <span class="usermenu__avatar usermenu__avatar--lg">{{ initial }}</span>
        <div class="usermenu__ident">
          <strong>{{ auth.username }}</strong>
          <div class="usermenu__roles">
            <span v-for="role in auth.roles" :key="role" class="role-chip">{{ role }}</span>
          </div>
        </div>
      </div>
      <router-link class="usermenu__item" to="/settings" role="menuitem" @click="open = false">
        <Settings :size="16" />
        Settings
      </router-link>
      <button class="usermenu__item usermenu__item--danger" role="menuitem" @click="signOut">
        <LogOut :size="16" />
        Sign out
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ChevronDown, LogOut, Settings } from "@lucide/vue";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();
const open = ref(false);
const root = ref<HTMLElement | null>(null);

const initial = computed(() => (auth.username[0] ?? "?").toUpperCase());

async function signOut() {
  open.value = false;
  await auth.logout();
  await router.push({ name: "login" });
}

function onDocClick(event: MouseEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) {
    open.value = false;
  }
}

onMounted(() => document.addEventListener("click", onDocClick));
onBeforeUnmount(() => document.removeEventListener("click", onDocClick));
</script>

<style scoped>
.usermenu {
  position: relative;
}
.usermenu__trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 38px;
  padding: 0 9px 0 7px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
}
.usermenu__trigger:hover {
  border-color: var(--border-strong);
  background: var(--panel-2);
}
.usermenu__avatar {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--accent-weak);
  color: var(--accent-strong);
  border: 1px solid rgba(139, 179, 255, 0.24);
  font-size: 12px;
  font-weight: 700;
}
.usermenu__avatar--lg {
  width: 34px;
  height: 34px;
  font-size: 15px;
}
.usermenu__name {
  flex: 1;
  font-size: 13px;
  text-align: left;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.usermenu__panel {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  width: 240px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--panel);
  box-shadow: var(--shadow);
  z-index: 40;
}
.usermenu__head {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 6px 8px 12px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 6px;
}
.usermenu__ident strong {
  font-size: 14px;
}
.usermenu__roles {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.role-chip {
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--accent-weak);
  color: var(--accent);
  font-size: 11px;
}
.usermenu__item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 8px 9px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  font-size: 13px;
  text-align: left;
  text-decoration: none;
  cursor: pointer;
}
.usermenu__item:hover {
  background: var(--panel-2);
}
.usermenu__item--danger {
  color: var(--danger);
}
@media (max-width: 900px) {
  .usermenu__trigger {
    width: 34px;
    height: 34px;
    padding: 0;
    justify-content: center;
    border: 0;
  }
  .usermenu__name,
  .usermenu__trigger > svg {
    display: none;
  }
  .usermenu__panel {
    right: 0;
    left: auto;
    top: calc(100% + 8px);
    bottom: auto;
  }
}
</style>
