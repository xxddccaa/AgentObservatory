import { createApp } from "vue";
import router from "./router";
import App from "./App.vue";
import "./style.css";

// Single local user, no login: the API is protected by its loopback bind and
// its Host / Origin checks, so there is no session to bootstrap here.
createApp(App).use(router).mount("#app");
