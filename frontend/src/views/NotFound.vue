<template>
  <div class="notfound">
    <p class="notfound__code">404</p>
    <h1>Page not found</h1>
    <p class="notfound__hint">The page <code>{{ path }}</code> doesn't exist.</p>
    <router-link class="notfound__link" to="/">← Back to dashboard</router-link>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();
const path = computed(() => route.fullPath);
</script>

<style scoped>
.notfound {
  max-width: 520px;
  margin: 12vh auto 0;
  padding: 0 20px;
  text-align: center;
}
.notfound__code {
  margin: 0;
  font: 700 64px/1 var(--mono);
  color: var(--accent);
  opacity: 0.6;
}
.notfound h1 {
  margin: 12px 0 6px;
  font-size: 22px;
}
.notfound__hint {
  color: var(--fg-muted);
}
.notfound__hint code {
  font-family: var(--mono);
  color: var(--fg);
}
.notfound__link {
  display: inline-block;
  margin-top: 18px;
}
</style>
