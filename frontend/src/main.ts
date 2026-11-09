import { createApp } from "vue";
import { createPinia } from "pinia";
import router from "./router";
import App from "./App.vue";
import { onUnauthorized } from "./api/client";
import { useAuthStore } from "./stores/auth";
import "./style.css";

const pinia = createPinia();
const app = createApp(App);
app.use(pinia);
app.use(router);

// Any API 401 (e.g. a session that expired mid-use) resets auth state and
// bounces to /login, preserving where the user was so they land back there.
const auth = useAuthStore(pinia);
onUnauthorized(() => {
  auth.markAnonymous();
  const current = router.currentRoute.value;
  if (current.name !== "login") {
    void router.push({ name: "login", query: { redirect: current.fullPath } });
  }
});

app.mount("#app");
