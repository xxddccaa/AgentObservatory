import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import { useAuthStore } from "../stores/auth";

const Dashboard = () => import("../views/Dashboard.vue");
const History = () => import("../views/History.vue");
const HistoryDetail = () => import("../views/HistoryDetail.vue");
const Memories = () => import("../views/Memories.vue");
const Audit = () => import("../views/Audit.vue");
const Settings = () => import("../views/Settings.vue");
const Insights = () => import("../views/Insights.vue");
const Projects = () => import("../views/Projects.vue");
const ProjectDetail = () => import("../views/ProjectDetail.vue");
const Guide = () => import("../views/Guide.vue");
const Storage = () => import("../views/Storage.vue");
const System = () => import("../views/System.vue");
const Login = () => import("../views/Login.vue");
const NotFound = () => import("../views/NotFound.vue");

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: Login,
    meta: { title: "Sign in", public: true, chromeless: true },
  },
  {
    path: "/",
    name: "dashboard",
    component: Dashboard,
    meta: { title: "Dashboard" },
  },
  { path: "/history", name: "history", component: History, meta: { title: "History" } },
  {
    path: "/memories",
    name: "memories",
    component: Memories,
    meta: { title: "Memories" },
  },
  {
    path: "/insights",
    name: "insights",
    component: Insights,
    meta: { title: "Insights" },
  },
  {
    path: "/projects",
    name: "projects",
    component: Projects,
    meta: { title: "Projects" },
  },
  {
    path: "/projects/:id",
    name: "project-detail",
    component: ProjectDetail,
    props: true,
    meta: { title: "Project" },
  },
  { path: "/guide", name: "guide", component: Guide, meta: { title: "使用方法" } },
  { path: "/stats", redirect: "/insights" },
  {
    path: "/history/:id",
    name: "history-detail",
    component: HistoryDetail,
    props: true,
    meta: { title: "History Session" },
  },
  { path: "/audit", name: "audit", component: Audit, meta: { title: "Audit" } },
  { path: "/settings", name: "settings", component: Settings, meta: { title: "Settings" } },
  {
    path: "/storage",
    name: "storage",
    component: Storage,
    meta: { title: "Storage", roles: ["admin", "operator"] },
  },
  {
    path: "/system",
    name: "system",
    component: System,
    meta: { title: "System", roles: ["admin"] },
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: NotFound,
    meta: { title: "Not found" },
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  // Resolve the session exactly once, before the first protected navigation.
  if (auth.status === "loading") await auth.bootstrap();

  const isPublic = to.meta.public === true;
  if (!isPublic && !auth.isAuthed) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  if (to.name === "login" && auth.isAuthed) {
    const redirect = typeof to.query.redirect === "string" ? to.query.redirect : "/";
    return redirect;
  }

  const requiredRoles = to.meta.roles as string[] | undefined;
  if (requiredRoles && !requiredRoles.some((role) => auth.roles.includes(role))) {
    return { name: "dashboard" };
  }
  return true;
});

router.afterEach((to) => {
  document.title = `${(to.meta.title as string) ?? "Agent Observatory"} — Agent Observatory`;
});

export default router;
