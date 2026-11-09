<template>
  <div class="settings">
    <header class="settings__header">
      <p class="eyebrow">Account & administration</p>
      <h1>Settings</h1>
      <p class="muted">Manage your account, access tokens, and (for admins) workspace users.</p>
    </header>

    <section class="card" aria-labelledby="profile-heading">
      <h2 id="profile-heading">Profile</h2>
      <p class="muted">Your current Observatory identity and permissions.</p>
      <dl class="profile">
        <dt>Username</dt><dd>{{ auth.username }}</dd>
        <dt>User ID</dt><dd class="mono value-wrap">{{ auth.principal?.id }}</dd>
        <dt>Roles</dt><dd><span v-for="role in auth.roles" :key="role" class="chip">{{ role }}</span></dd>
      </dl>
    </section>

    <section class="card" aria-labelledby="security-heading">
      <h2 id="security-heading">Security</h2>
      <p class="muted">Change your password. Use at least 10 characters.</p>
      <form class="form-grid" @submit.prevent="submitPassword">
        <label><span>Current password</span><input v-model="passwordForm.current" type="password" autocomplete="current-password" required /></label>
        <label><span>New password</span><input v-model="passwordForm.next" type="password" autocomplete="new-password" minlength="10" required /></label>
        <label><span>Confirm new password</span><input v-model="passwordForm.confirm" type="password" autocomplete="new-password" minlength="10" required /></label>
        <div class="form-actions"><button class="primary-btn" type="submit" :disabled="passwordBusy">{{ passwordBusy ? "Updating…" : "Change password" }}</button><span v-if="passwordNotice" class="success-text">{{ passwordNotice }}</span></div>
      </form>
      <p v-if="passwordError" class="error-text">{{ passwordError }}</p>
    </section>

    <section class="card" aria-labelledby="tokens-heading">
      <div class="section-heading"><div><h2 id="tokens-heading">API tokens</h2><p class="muted">Create a bearer token for scripts. The secret is shown exactly once.</p></div><button class="ghost-btn" type="button" :disabled="tokensBusy" @click="loadTokens">Refresh</button></div>
      <form class="form-grid" @submit.prevent="createToken">
        <label><span>Token name</span><input v-model.trim="tokenName" placeholder="e.g. ci-runner" required /></label>
        <label><span>Expiry (days, optional)</span><input v-model.number="tokenDays" type="number" min="1" max="3650" placeholder="Never" /></label>
        <div class="form-actions"><button class="primary-btn" type="submit" :disabled="tokenBusy">{{ tokenBusy ? "Creating…" : "Create token" }}</button></div>
      </form>
      <p v-if="tokenError" class="error-text">{{ tokenError }}</p>
      <div v-if="createdToken" class="token-reveal"><strong>Copy this token now</strong><span class="muted">It will not be shown again.</span><div class="token-row"><code class="mono">{{ createdToken.token }}</code><button class="ghost-btn" type="button" @click="copy(createdToken.token)">Copy</button></div></div>
      <div v-if="tokensLoading" class="empty-state">Loading tokens…</div>
      <div v-else-if="tokens.length === 0" class="empty-state">No API tokens have been created.</div>
      <ul v-else class="token-list"><li v-for="token in tokens" :key="token.id" class="token-item"><div><strong>{{ token.name }}</strong><span class="muted token-meta">Created {{ formatDateTime(token.created_at) }}<template v-if="token.expires_at"> · expires {{ formatDateTime(token.expires_at) }}</template><template v-if="token.last_used_at"> · last used {{ formatDateTime(token.last_used_at) }}</template></span></div><div class="token-actions"><span v-if="token.revoked_at" class="badge" data-tone="muted">Revoked</span><button v-else class="ghost-btn danger-btn" type="button" @click="revokeToken(token.id, token.name)">Revoke</button></div></li></ul>
    </section>

    <section v-if="auth.isAdmin" class="card" aria-labelledby="users-heading">
      <div class="section-heading"><div><h2 id="users-heading">Users & roles</h2><p class="muted">Admin-only account management. Changes are audited server-side.</p></div><button class="ghost-btn" type="button" :disabled="usersLoading" @click="loadUsers">Refresh</button></div>
      <form class="create-user form-grid" @submit.prevent="submitUser">
        <label><span>Username</span><input v-model.trim="newUser.username" autocomplete="off" required /></label>
        <label><span>Temporary password</span><input v-model="newUser.password" type="password" minlength="10" autocomplete="new-password" required /></label>
        <fieldset class="role-fieldset"><legend>Roles</legend><label v-for="role in allRoles" :key="role" class="role-check"><input type="checkbox" :value="role" v-model="newUser.roles" /> {{ role }}</label></fieldset>
        <div class="form-actions"><button class="primary-btn" type="submit" :disabled="userBusy">{{ userBusy ? "Creating…" : "Add user" }}</button></div>
      </form>
      <p v-if="userError" class="error-text">{{ userError }}</p><p v-if="usersError" class="error-text">{{ usersError }}</p>
      <div v-if="usersLoading" class="empty-state">Loading users…</div><div v-else-if="users.length === 0" class="empty-state">No users found.</div>
      <div v-else class="users-wrap"><table class="users"><thead><tr><th>Username</th><th>Roles</th><th>Status</th><th>Created</th><th>Action</th></tr></thead><tbody><tr v-for="user in users" :key="user.id"><td><strong>{{ user.username }}</strong><span v-if="user.id === auth.principal?.id" class="muted self-label">You</span></td><td><div class="role-list"><label v-for="role in allRoles" :key="role" class="role-check compact"><input type="checkbox" :checked="draft(user).roles.includes(role)" @change="toggleRole(user, role)" /> {{ role }}</label></div></td><td><button class="status-btn" :class="{ active: draft(user).is_active }" type="button" :disabled="user.id === auth.principal?.id || updatingUser === user.id" @click="toggleActive(user)">{{ draft(user).is_active ? "Active" : "Disabled" }}</button></td><td class="muted">{{ formatDate(user.created_at) }}</td><td><button class="ghost-btn" type="button" :disabled="updatingUser === user.id" @click="saveUser(user)">{{ updatingUser === user.id ? "Saving…" : "Save" }}</button></td></tr></tbody></table></div>
    </section>
    <section v-else class="card muted-info"><strong>Administrator settings</strong><span>Only administrators can manage users and roles.</span></section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useAuthStore } from "../stores/auth";
import { changePassword, createApiToken, listApiTokens, revokeApiToken, type CreatedToken, type TokenInfo } from "../api/auth";
import { createUser, listUsers, updateUser, type AdminUser } from "../api/admin";
import { ApiError } from "../api/client";

const auth = useAuthStore();
const allRoles = ["admin", "operator", "viewer"];
const passwordForm = reactive({ current: "", next: "", confirm: "" });
const passwordBusy = ref(false); const passwordError = ref(""); const passwordNotice = ref("");
async function submitPassword() { passwordError.value = ""; passwordNotice.value = ""; if (passwordForm.next !== passwordForm.confirm) { passwordError.value = "New passwords do not match."; return; } passwordBusy.value = true; try { await changePassword(passwordForm.current, passwordForm.next); passwordForm.current = ""; passwordForm.next = ""; passwordForm.confirm = ""; passwordNotice.value = "Password changed."; } catch (cause) { passwordError.value = cause instanceof Error ? cause.message : "Failed to change password"; } finally { passwordBusy.value = false; } }
const tokenName = ref(""); const tokenDays = ref<number | null>(null); const tokenBusy = ref(false); const tokensBusy = ref(false); const tokenError = ref(""); const tokens = ref<TokenInfo[]>([]); const tokensLoading = ref(true); const createdToken = ref<CreatedToken | null>(null);
async function loadTokens() { tokensLoading.value = true; tokensBusy.value = true; tokenError.value = ""; try { tokens.value = await listApiTokens(); } catch (cause) { tokenError.value = cause instanceof Error ? cause.message : "Failed to load tokens"; } finally { tokensLoading.value = false; tokensBusy.value = false; } }
async function createToken() { if (tokenBusy.value) return; tokenError.value = ""; tokenBusy.value = true; try { createdToken.value = await createApiToken(tokenName.value, tokenDays.value ?? undefined); tokenName.value = ""; tokenDays.value = null; await loadTokens(); } catch (cause) { tokenError.value = cause instanceof Error ? cause.message : "Failed to create token"; } finally { tokenBusy.value = false; } }
async function revokeToken(id: string, name: string) { if (!window.confirm(`Revoke token "${name}"? Existing clients will stop working.`)) return; try { await revokeApiToken(id); await loadTokens(); } catch (cause) { tokenError.value = cause instanceof Error ? cause.message : "Failed to revoke token"; } }
const users = ref<AdminUser[]>([]); const usersLoading = ref(false); const usersError = ref(""); const newUser = reactive({ username: "", password: "", roles: ["viewer"] as string[] }); const userBusy = ref(false); const userError = ref(""); const drafts = reactive<Record<string, { roles: string[]; is_active: boolean }>>({}); const updatingUser = ref<string | null>(null);
function setDraft(user: AdminUser) { drafts[user.id] = { roles: [...user.roles], is_active: user.is_active }; } function draft(user: AdminUser) { if (!drafts[user.id]) setDraft(user); return drafts[user.id]; } function toggleRole(user: AdminUser, role: string) { const current = draft(user); current.roles = current.roles.includes(role) ? current.roles.filter((item) => item !== role) : [...current.roles, role]; } function toggleActive(user: AdminUser) { draft(user).is_active = !draft(user).is_active; }
async function loadUsers() { if (!auth.isAdmin) return; usersLoading.value = true; usersError.value = ""; try { users.value = (await listUsers()).users; users.value.forEach(setDraft); } catch (cause) { usersError.value = cause instanceof Error ? cause.message : "Failed to load users"; } finally { usersLoading.value = false; } }
async function saveUser(user: AdminUser) { const next = draft(user); if (!next.roles.length) { usersError.value = "Each user needs at least one role."; return; } updatingUser.value = user.id; usersError.value = ""; try { const updated = await updateUser(user.id, next); const index = users.value.findIndex((item) => item.id === user.id); if (index >= 0) users.value[index] = updated; setDraft(updated); } catch (cause) { usersError.value = cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : "Failed to update user"; } finally { updatingUser.value = null; } }
async function submitUser() { userError.value = ""; if (!newUser.roles.length) { userError.value = "Choose at least one role."; return; } userBusy.value = true; try { await createUser(newUser); newUser.username = ""; newUser.password = ""; newUser.roles = ["viewer"]; await loadUsers(); } catch (cause) { userError.value = cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : "Failed to create user"; } finally { userBusy.value = false; } }
function formatDate(value: string | null) { return value ? new Date(value).toLocaleDateString() : "-"; } function formatDateTime(value: string | null) { return value ? new Date(value).toLocaleString() : "never"; } async function copy(value: string) { try { await navigator.clipboard.writeText(value); } catch { /* clipboard unavailable */ } }
onMounted(() => { void loadTokens(); void loadUsers(); });
</script>

<style scoped>
.settings { max-width: 920px; margin: 0 auto; padding: 28px 22px 48px; }.settings__header { margin-bottom: 18px; }.eyebrow { margin: 0 0 4px; color: var(--fg-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }h1 { margin: 0; font-size: 25px; }.card { margin-bottom: 16px; padding: 18px 20px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.card h2 { margin: 0 0 4px; font-size: 16px; }.muted { color: var(--fg-muted); font-size: 13px; }.profile { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 8px 16px; margin: 12px 0 0; }.profile dt { color: var(--fg-muted); font-size: 13px; }.profile dd { min-width: 0; margin: 0; }.value-wrap { overflow-wrap: anywhere; }.mono { font-family: var(--mono); }.chip { display: inline-block; margin-right: 5px; padding: 1px 8px; border-radius: 999px; background: var(--accent-weak); color: var(--accent); font-size: 12px; }.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); align-items: end; gap: 10px; margin-top: 12px; }.form-grid label, .role-fieldset { display: flex; min-width: 0; flex-direction: column; gap: 5px; }.form-grid label > span, .role-fieldset legend { color: var(--fg-muted); font-size: 12px; }.form-grid input { box-sizing: border-box; width: 100%; height: 36px; padding: 0 11px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg); color: var(--fg); outline: none; }.form-grid input:focus { border-color: var(--accent); }.form-actions { display: flex; align-items: center; gap: 10px; min-height: 36px; }.primary-btn, .ghost-btn, .status-btn { min-height: 34px; padding: 0 12px; border-radius: 7px; cursor: pointer; }.primary-btn { border: 0; background: var(--accent); color: #fff; font-weight: 600; }.primary-btn:disabled, .ghost-btn:disabled, .status-btn:disabled { opacity: .55; cursor: default; }.ghost-btn, .status-btn { border: 1px solid var(--border); background: var(--panel); color: var(--fg); }.danger-btn { color: var(--danger); }.status-btn.active { border-color: var(--ok); color: var(--ok); }.error-text { color: var(--danger); font-size: 13px; }.success-text { color: var(--ok); font-size: 13px; }.empty-state { padding: 14px 0 2px; color: var(--fg-muted); font-size: 13px; }.token-reveal { display: grid; gap: 4px; margin: 12px 0; padding: 12px; border: 1px solid var(--ok); border-radius: 8px; background: var(--ok-weak); }.token-row { display: flex; align-items: center; gap: 10px; min-width: 0; }.token-row code { min-width: 0; flex: 1; overflow: auto; padding: 6px 8px; border-radius: 5px; background: var(--bg); font-size: 12px; }.token-list { display: grid; gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }.token-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 0; border-top: 1px solid var(--border); }.token-meta { display: block; margin-top: 3px; }.token-actions { flex: 0 0 auto; }.create-user { padding-bottom: 14px; border-bottom: 1px solid var(--border); }.role-fieldset { min-height: 36px; box-sizing: border-box; flex-flow: row wrap; align-content: center; border: 0; padding: 0; margin: 0; }.role-fieldset legend { width: 100%; }.role-check { display: inline-flex; align-items: center; gap: 5px; color: var(--fg-muted); font-size: 13px; }.role-check input { width: auto; height: auto; }.users-wrap { overflow-x: auto; }.users { width: 100%; min-width: 640px; border-collapse: collapse; margin-top: 12px; font-size: 13px; }.users th, .users td { text-align: left; vertical-align: top; padding: 9px 8px; border-bottom: 1px solid var(--border); }.users th { color: var(--fg-muted); font-weight: 500; }.role-list { display: flex; flex-wrap: wrap; gap: 5px 9px; }.compact { white-space: nowrap; font-size: 12px; }.self-label { display: block; margin-top: 3px; }.muted-info { display: flex; flex-wrap: wrap; gap: 5px 8px; }
@media (max-width: 560px) { .settings { padding: 20px 14px 36px; }.card { padding: 15px; }.profile { grid-template-columns: 90px minmax(0, 1fr); gap: 7px 10px; }.section-heading { align-items: stretch; flex-direction: column; }.section-heading > .ghost-btn { align-self: flex-start; }.token-item { align-items: flex-start; flex-direction: column; } }
</style>
