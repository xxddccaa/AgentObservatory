<template>
  <router-view v-if="chromeless" />
  <AppShell v-else>
    <router-view />
  </AppShell>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import AppShell from "./components/AppShell.vue";

const route = useRoute();
// Login (and other chromeless routes) render full-bleed without the app shell.
const chromeless = computed(() => route.meta.chromeless === true);
</script>
