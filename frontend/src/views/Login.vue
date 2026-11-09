<template>
  <div class="login">
    <div class="login__masthead" aria-hidden="true">
      <span class="login__mark"><Telescope :size="22" /></span>
      <span>Agent Observatory</span>
    </div>
    <form class="login__card" @submit.prevent="submit">
      <div class="login__brand">
        <div>
          <p class="login__eyebrow">Local workspace access</p>
          <h1>Sign in</h1>
          <p class="login__sub">Continue to your agent sessions and audit history.</p>
        </div>
      </div>

      <label class="field">
        <span>Username</span>
        <input
          ref="usernameInput"
          v-model.trim="username"
          name="username"
          autocomplete="username"
          autocapitalize="off"
          spellcheck="false"
          required
        />
      </label>

      <label class="field">
        <span>Password</span>
        <input
          v-model="password"
          name="password"
          type="password"
          autocomplete="current-password"
          required
        />
      </label>

      <div v-if="issue" class="login__error" role="alert">
        <strong>{{ issue.title }}</strong>
        <span>{{ issue.detail }}</span>
        <code v-if="issue.recoveryCommand">{{ issue.recoveryCommand }}</code>
        <small v-if="issue.requestId">Request ID: {{ issue.requestId }}</small>
        <button v-if="issue.retryable" type="button" :disabled="busy" @click="submit">
          Retry
        </button>
      </div>

      <button class="login__submit" type="submit" :disabled="busy">
        {{ busy ? "Signing in…" : "Sign in" }}
      </button>

      <p class="login__hint" :data-online="online">
        <span class="login__status-dot" />
        {{ envLabel }}
      </p>
    </form>
    <p class="login__foot">Local-first · audited access · role-based controls</p>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Telescope } from "@lucide/vue";
import { useAuthStore } from "../stores/auth";
import { getReadyHealth, type ReadyHealth } from "../api/health";
import { presentLoginError, type ActionableError } from "../api/error-presenter";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();

const username = ref("");
const password = ref("");
const issue = ref<ActionableError | null>(null);
const busy = ref(false);
const usernameInput = ref<HTMLInputElement | null>(null);
const online = ref(false);
const envLabel = ref("checking API…");
const readiness = ref<ReadyHealth | null>(null);

async function submit() {
  if (busy.value) return;
  issue.value = null;
  busy.value = true;
  try {
    await auth.login(username.value, password.value);
    const redirect = typeof route.query.redirect === "string" ? route.query.redirect : "/";
    await router.replace(redirect);
  } catch (cause) {
    issue.value = presentLoginError(cause, readiness.value);
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  usernameInput.value?.focus();
  try {
    const health = await getReadyHealth();
    readiness.value = health;
    online.value = health.status === "ok";
    envLabel.value = `${health.environment} · API ${health.status}`;
  } catch {
    readiness.value = null;
    online.value = false;
    envLabel.value = "API offline";
  }
});
</script>

<style scoped>
.login {
  min-height: 100dvh;
  display: grid;
  grid-template-rows: auto 1fr auto;
  place-items: center;
  padding: 24px;
  background: var(--bg);
}
.login__masthead {
  display: flex;
  align-items: center;
  justify-self: start;
  gap: 10px;
  color: var(--fg);
  font-size: 13px;
  font-weight: 650;
}
.login__mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border: 1px solid rgba(139, 179, 255, 0.24);
  border-radius: 7px;
  background: var(--accent-weak);
  color: var(--accent-strong);
}
.login__card {
  width: 100%;
  max-width: 400px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 30px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
}
.login__brand {
  padding-bottom: 2px;
}
.login__brand h1 {
  margin: 0 0 6px;
  font-size: 23px;
  font-weight: 650;
}
.login__eyebrow {
  margin: 0 0 5px;
  color: var(--accent-strong);
  font-size: 10px;
  font-weight: 650;
  text-transform: uppercase;
}
.login__sub {
  margin: 0;
  color: var(--fg-muted);
  font-size: 13px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--fg-muted);
}
.field input {
  height: 42px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: #101210;
  color: var(--fg);
  font-size: 14px;
  outline: none;
}
.field input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-weak);
}
.login__error {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 9px 11px;
  border: 1px solid var(--danger);
  border-radius: 7px;
  background: var(--danger-weak);
  color: var(--danger);
  font-size: 13px;
}
.login__error strong,
.login__error span {
  display: block;
}
.login__error code {
  max-width: 100%;
  margin-top: 4px;
  padding: 4px 6px;
  border-radius: 4px;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--mono);
  overflow-wrap: anywhere;
  user-select: all;
}
.login__error small {
  color: var(--fg-muted);
  overflow-wrap: anywhere;
  user-select: all;
}
.login__error button {
  margin-top: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--danger);
  font: inherit;
  font-weight: 700;
  text-decoration: underline;
  cursor: pointer;
}
.login__submit {
  height: 42px;
  border: 0;
  border-radius: 7px;
  background: #477edb;
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}
.login__submit:disabled {
  opacity: 0.6;
  cursor: default;
}
.login__hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  margin: 0;
  font-size: 11px;
  text-transform: uppercase;
  color: var(--fg-muted);
}
.login__hint[data-online="true"] {
  color: var(--ok);
}
.login__status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.login__foot {
  align-self: end;
  margin: 0;
  color: var(--fg-subtle);
  font-size: 11px;
}
@media (max-width: 520px) {
  .login {
    padding: 18px;
  }
  .login__card {
    padding: 24px 20px;
  }
  .login__foot {
    text-align: center;
  }
}
</style>
